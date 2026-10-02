"""Панель «Режим администратора» (pre14): положение объектов страницы из GUI.

Отдельный модуль, чтобы main.py изменился минимально: в нём только импорт,
создание панели и передача значений в :mod:`layout_tune`.

Панель — измерительный инструмент, а не часть продукта:
* ползунки меняют сдвиги СРАЗУ (с небольшим антидребезгом), чтобы администратор
  видел результат движением, а не по логам;
* «Авто: по видимой области» считает дельты ширины/высоты арифметически —
  раскладка становится равна видимому слоту окна (413x651 DIP при 67 %);
* «Вывод результатов» печатает итоговые числа, кладёт их в буфер обмена и
  пишет в ``%APPDATA%\\Legalyze\\layout_tune_result.txt`` — этот текст и нужно
  передать разработчику, чтобы значения вшили в постоянную сборку.
"""

from __future__ import annotations

import json
import threading
import time

import diagnostics as diag
import layout_tune

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QSlider, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

PANEL_STYLE = """
QWidget#adminPanel {
    background: #12142a;
    color: #e6e8f5;
    font-size: 11px;
}
QLabel#adminTitle {
    color: #7c83ff;
    font-size: 13px;
    font-weight: 800;
}
QLabel#adminHint {
    color: #8f96bf;
    font-size: 10px;
}
QLabel#adminValue {
    color: #e6e8f5;
    font-size: 11px;
    font-weight: 700;
}
QSlider::groove:horizontal {
    background: #262a4a; height: 4px; border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #7c83ff; width: 12px; margin: -5px 0; border-radius: 6px;
}
QSpinBox {
    background: #1b1e3a; color: #e6e8f5; border: 1px solid #262a4a;
    border-radius: 5px; padding: 2px 4px; min-width: 58px;
}
QLineEdit {
    background: #1b1e3a; color: #e6e8f5; border: 1px solid #262a4a;
    border-radius: 5px; padding: 4px 6px;
}
QPushButton {
    background: rgba(255, 255, 255, 0.04); color: #e6e8f5;
    border: 1px solid #262a4a; border-radius: 7px;
    padding: 6px 10px; font-weight: 600;
}
QPushButton:hover { background: rgba(124, 131, 255, 0.24); border-color: #7c83ff; }
QPushButton:pressed { background: rgba(124, 131, 255, 0.4); }
QTextEdit {
    background: #0e1020; color: #c9cdea; border: 1px solid #262a4a;
    border-radius: 7px; font-family: Consolas, monospace; font-size: 10px;
}
"""


class _Knob(QWidget):
    """Ползунок + числовое поле + подпись значения."""

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        low, high = layout_tune.LIMITS[key]
        divisor = layout_tune.DIVISOR[key]
        unit = layout_tune.UNITS[key]

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        caption = QLabel(layout_tune.LABELS[key])
        caption.setMinimumWidth(150)
        row.addWidget(caption)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(low), int(high))
        self.slider.setSingleStep(int(layout_tune.STEPS[key]))
        self.slider.setPageStep(max(1, (int(high) - int(low)) // 20))
        row.addWidget(self.slider, 1)

        self.spin = QSpinBox()
        self.spin.setRange(int(low), int(high))
        self.spin.setFixedWidth(70)
        row.addWidget(self.spin)

        self.value_label = QLabel("")
        self.value_label.setObjectName("adminValue")
        self.value_label.setFixedWidth(78)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.value_label)

        self._unit = unit
        self._divisor = float(divisor)
        self._muted = False

        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    # ------------------------------------------------------------------ sync
    def _from_slider(self, value):
        if self._muted:
            return
        self._muted = True
        try:
            self.spin.setValue(int(value))
        finally:
            self._muted = False
        self._refresh()
        self.value_changed()

    def _from_spin(self, value):
        if self._muted:
            return
        self._muted = True
        try:
            self.slider.setValue(int(value))
        finally:
            self._muted = False
        self._refresh()
        self.value_changed()

    def value_changed(self):
        parent = self.parent()
        while parent is not None and not isinstance(parent, AdminPanel):
            parent = parent.parent()
        if isinstance(parent, AdminPanel):
            parent._knob_changed()

    # ---------------------------------------------------------------- value
    def set_value(self, raw):
        self._muted = True
        try:
            self.slider.setValue(int(raw))
            self.spin.setValue(int(raw))
        finally:
            self._muted = False
        self._refresh()

    def value(self):
        return int(self.spin.value())

    def _refresh(self):
        shown = float(self.spin.value()) / self._divisor
        if self._divisor == 10.0:
            text = "%.1f %s" % (shown, self._unit)
        else:
            text = "%d %s" % (int(round(shown)), self._unit)
        self.value_label.setText(text)


class AdminPanel(QWidget):
    """Окно настройки положения объектов страницы (открывается кнопкой ⚙ или F8)."""

    _done = pyqtSignal(object, object)

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.tune = layout_tune.normalize(getattr(owner, "tune", None))
        if layout_tune.is_default(self.tune):
            # Панель открыта — значит администратор пришёл настраивать: сразу
            # показываем рамку видимой области, чтобы было видно цель.
            self.tune["frame"] = 1
        self._busy = False
        self._last_report = ""
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        # 350 мс: пока ползунок тянут, применение не стартует на каждое
        # деление шкалы — иначе применения накладываются друг на друга.
        self._debounce.setInterval(350)
        self._debounce.timeout.connect(self._push)

        self.setObjectName("adminPanel")
        self.setStyleSheet(PANEL_STYLE)
        self.setWindowTitle("Legalyze — режим администратора")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.resize(430, 780)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        title = QLabel("Режим администратора — положение объектов")
        title.setObjectName("adminTitle")
        root.addWidget(title)

        hint = QLabel(
            "pre14: сдвиги только для подбора. Сначала двигайте ОКНО БРАУЗЕРА "
            "(оно и есть причина «поле уехало вниз»), затем — содержимое. "
            "«Вывод результатов» → разработчику.")
        hint.setObjectName("adminHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.live_box = QCheckBox("Применять сразу при движении ползунка")
        self.live_box.setChecked(True)
        root.addWidget(self.live_box)

        self.frame_box = QCheckBox("Показывать рамку видимой области (красный пунктир)")
        self.frame_box.setChecked(bool(self.tune.get("frame", 0)))
        self.frame_box.stateChanged.connect(self._knob_changed)
        root.addWidget(self.frame_box)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        holder = QWidget()
        holder.setObjectName("adminPanel")
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        self.knobs = {}
        for row in layout_tune.SPEC:
            key = row[0]
            knob = _Knob(key, holder)
            knob.set_value(self.tune.get(key, layout_tune.DEFAULTS[key]))
            self.knobs[key] = knob
            column.addWidget(knob)

        column.addWidget(QLabel("Селектор поля ввода:"))
        self.selector_edit = QLineEdit(self.tune.get("selector", layout_tune.INPUT_SELECTOR))
        self.selector_edit.textChanged.connect(self._knob_changed)
        column.addWidget(self.selector_edit)

        column.addStretch(1)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.btn_apply = QPushButton("Применить")
        self.btn_apply.setToolTip("Применить принудительно (даже если замер совпал)")
        self.btn_apply.clicked.connect(lambda _c=False: self._push(force=True))
        buttons.addWidget(self.btn_apply)
        self.btn_auto = QPushButton("Авто: окно в слот")
        self.btn_auto.setToolTip(
            "Поставить окно браузера ровно в видимую область (413x651 DIP: "
            "сверху 54, снизу 30, по бокам 20)")
        self.btn_auto.clicked.connect(lambda _c=False: self._auto())
        buttons.addWidget(self.btn_auto)
        self.btn_measure = QPushButton("Измерить")
        self.btn_measure.clicked.connect(lambda _c=False: self._measure())
        buttons.addWidget(self.btn_measure)
        root.addLayout(buttons)

        buttons2 = QHBoxLayout()
        buttons2.setSpacing(6)
        self.btn_report = QPushButton("Вывод результатов")
        self.btn_report.clicked.connect(lambda _c=False: self._report())
        buttons2.addWidget(self.btn_report)
        self.btn_copy = QPushButton("Копировать")
        self.btn_copy.clicked.connect(lambda _c=False: self._copy())
        buttons2.addWidget(self.btn_copy)
        self.btn_reset = QPushButton("Сброс")
        self.btn_reset.clicked.connect(lambda _c=False: self._reset())
        buttons2.addWidget(self.btn_reset)
        root.addLayout(buttons2)

        buttons3 = QHBoxLayout()
        buttons3.setSpacing(6)
        self.btn_save = QPushButton("Сохранить в конфиг")
        self.btn_save.clicked.connect(lambda _c=False: self._save())
        buttons3.addWidget(self.btn_save)
        self.btn_hide = QPushButton("Скрыть (F8)")
        self.btn_hide.clicked.connect(lambda _c=False: self.hide())
        buttons3.addWidget(self.btn_hide)
        root.addLayout(buttons3)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFixedHeight(230)
        mono = QFont("Consolas", 9)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.output.setFont(mono)
        self.output.setPlainText(
            "Нажмите «Измерить», чтобы увидеть текущие числа, или «Вывод результатов»,\n"
            "чтобы получить готовый блок для отправки разработчику."
        )
        root.addWidget(self.output)

        self._done.connect(self._on_done)

    # ------------------------------------------------------------------ read
    def collect(self):
        tune = {}
        for key in layout_tune.NUM_KEYS:
            tune[key] = self.knobs[key].value()
        tune["selector"] = self.selector_edit.text().strip() or layout_tune.INPUT_SELECTOR
        tune["frame"] = 1 if self.frame_box.isChecked() else 0
        return layout_tune.normalize(tune)

    def sync_from(self, tune):
        """Перерисовать ползунки внешними значениями (например, после «Авто»)."""
        tune = layout_tune.normalize(tune)
        self.tune = tune
        for key in layout_tune.NUM_KEYS:
            self.knobs[key].set_value(tune[key])
        self.selector_edit.blockSignals(True)
        self.selector_edit.setText(tune["selector"])
        self.selector_edit.blockSignals(False)
        self.frame_box.blockSignals(True)
        self.frame_box.setChecked(bool(tune["frame"]))
        self.frame_box.blockSignals(False)

    # ----------------------------------------------------------------- apply
    def _knob_changed(self, *_args):
        self.tune = self.collect()
        if self.live_box.isChecked():
            self._debounce.start()

    def _push(self, force=False):
        """Поставить значение в очередь. `force` — только по кнопке «Применить».

        Живое применение НЕ форсирует: если замер уже совпадает с целью,
        страницу не трогают вовсе (меньше перерисовок — меньше «миганий»).
        """
        self._debounce.stop()
        self.tune = self.collect()
        try:
            self.owner.tune = self.tune
            self.owner.apply_tune(force=force)
            self._status("Применено: " + self._short())
        except Exception:
            diag.exception("layout_tune_ui.push")

    def _auto(self):
        try:
            geom = self.owner.tune_geom()
        except Exception:
            diag.exception("layout_tune_ui.geom")
            geom = None
        filled = layout_tune.auto_fill(geom, self.collect())
        self.sync_from(filled)
        rect = layout_tune.window_rect(geom, filled)
        self.output.setPlainText(
            "Окно браузера поставлено в видимый слот: x %d..%d, y %d..%d (%dx%d DIP).\n"
            "Раскладка осталась равной самому окну, поэтому пустых полос нет.\n"
            "Дальше доведите ползунками: цель — «ниже видимой нижней кромки» <= 0."
            % (rect[0], rect[0] + rect[2], rect[1], rect[1] + rect[3],
               rect[2], rect[3]))
        self._push()

    def _short(self):
        values = layout_tune.as_floats(self.tune)
        return ("win=(%s,%s,%s,%s) zoom=%s width_delta=%s height_delta=%s "
                "scroll_y=%s offset=(%s,%s) input=(%s,%s) pad_bottom=%s"
                % (values["win_dx"], values["win_dy"], values["win_dw"],
                   values["win_dh"], values["zoom"], values["width_delta"],
                   values["height_delta"], values["scroll_y"], values["offset_x"],
                   values["offset_y"], values["input_dx"], values["input_dy"],
                   values["pad_bottom"]))

    # --------------------------------------------------------------- measure
    def _run_async(self, token, fn):
        if self._busy:
            return
        self._busy = True
        self._set_buttons(False)

        def task():
            value = None
            try:
                value = fn()
            except Exception:
                diag.exception("layout_tune_ui.task")
            self._done.emit(token, value)

        threading.Thread(target=task, daemon=True).start()

    def _set_buttons(self, enabled):
        for button in (self.btn_apply, self.btn_auto, self.btn_measure,
                       self.btn_report, self.btn_save, self.btn_reset):
            button.setEnabled(enabled)

    def _page(self):
        try:
            return self.owner.current_page()
        except Exception:
            diag.exception("layout_tune_ui.page")
            return None

    def _geom(self):
        try:
            return self.owner.tune_geom()
        except Exception:
            diag.exception("layout_tune_ui.geom")
            return {}

    def _measure(self):
        page = self._page()
        if page is None:
            self.output.setPlainText("Страница ещё не подключена (CDP недоступен).")
            return
        tune = self.collect()
        geom = self._geom()
        self._run_async("measure", lambda: layout_tune.measure(page, tune, geom))

    def _report(self):
        page = self._page()
        if page is None:
            self.output.setPlainText("Страница ещё не подключена (CDP недоступен).")
            return
        self.tune = self.collect()
        tune = self.tune
        geom = self._geom()

        def task():
            measured = layout_tune.measure(page, tune, geom)
            return layout_tune.report(tune, measured, geom)

        self._run_async("report", task)

    def _on_done(self, token, value):
        self._busy = False
        self._set_buttons(True)
        if token == "measure" and isinstance(value, dict):
            self.output.setPlainText(self._format_measure(value))
            return
        if token == "report" and isinstance(value, str):
            self._last_report = value
            self.output.setPlainText(value)
            self._copy(silent=True)
            self._write_result(value)
            diag.event("layout.tune_report",
                       values=json.dumps(layout_tune.as_floats(self.tune),
                                         ensure_ascii=False))
            return

    def _format_measure(self, measured):
        inner = measured.get("inner") or {}
        visible = measured.get("visible_dip") or {}
        box = measured.get("input") or {}
        lines = [
            "Раскладка css %sx%s, dsf %s, зум %s"
            % (measured.get("css", ("?", "?"))[0], measured.get("css", ("?", "?"))[1],
               measured.get("dsf"), measured.get("zoom")),
            "Окно DIP %sx%s, рамка %s"
            % (measured.get("logical", (0, 0))[0], measured.get("logical", (0, 0))[1],
               measured.get("frame")),
            "Видимо (DIP): x %s..%s, y %s..%s"
            % (visible.get("left"), visible.get("right"),
               visible.get("top"), visible.get("bottom")),
            "innerWidth=%s innerHeight=%s dpr=%s scrollWidth=%s"
            % (inner.get("w"), inner.get("h"), inner.get("dpr"), inner.get("sw")),
            "",
        ]
        if box.get("found"):
            lines.append("Поле ввода: %s" % (box.get("selector") or "?"))
            lines.append("  css: %s" % (box.get("css") or {}))
            lines.append("  DIP: %s" % (box.get("dip") or {}))
            lines.append("  ниже видимой кромки: %s DIP (норма <= 0)"
                         % measured.get("below_dip"))
            lines.append("  выше видимой кромки: %s DIP (норма <= 0)"
                         % measured.get("above_dip"))
            lines.append("  слева/справа: %s / %s DIP (норма <= 0)"
                         % (measured.get("left_dip"), measured.get("right_dip")))
        else:
            lines.append("Поле ввода НЕ найдено: %s" % self.tune.get("selector"))
        lines.append("overflow_x = %s css px" % measured.get("overflow_x"))
        return "\n".join(lines)

    def _status(self, text):
        self.output.setPlainText(text + "\n\n" + self.output.toPlainText())

    # ------------------------------------------------------------ clipboard
    def _copy(self, silent=False):
        text = self._last_report or self.output.toPlainText()
        if not text:
            return
        try:
            QApplication.clipboard().setText(text)
            if not silent:
                self._status("Скопировано в буфер обмена.")
        except Exception:
            diag.exception("layout_tune_ui.clipboard")

    def _write_result(self, text):
        """Дублируем отчёт в файл: буфер обмена на чужой машине может быть занят."""
        try:
            from config import APP_DIR
            path = APP_DIR / "layout_tune_result.txt"
            APP_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "%s\n\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text),
                encoding="utf-8",
            )
            self._status("Отчёт также записан: %s" % path)
        except Exception:
            diag.exception("layout_tune_ui.write_result")

    # ----------------------------------------------------------------- misc
    def _reset(self):
        self.sync_from(layout_tune.DEFAULTS)
        self._push()
        self.output.setPlainText("Сброшено к значениям v13 (все сдвиги 0, масштаб 66.7 %).")

    def _save(self):
        self.tune = self.collect()
        try:
            owner = self.owner
            owner.tune = self.tune
            if hasattr(owner, "save_tune"):
                owner.save_tune()
            self._status("Сохранено в config.json (ключ layout_tune).")
        except Exception:
            diag.exception("layout_tune_ui.save")

    def showEvent(self, event):
        super().showEvent(event)
        try:
            self.raise_()
            self.activateWindow()
        except Exception:
            diag.exception("layout_tune_ui.show")

    def closeEvent(self, event):
        try:
            self._debounce.stop()
        except Exception:
            diag.exception("layout_tune_ui.close")
        super().closeEvent(event)
