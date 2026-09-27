"""Legalyze Decrypt Tool — проверка/расшифровка .enc файлов с сервера.

ТЗ 1.3: понимает новый формат (зашифрованный PDF) и legacy-формат.
Расширение результата выбирается автоматически: %PDF -> .pdf, иначе -> .txt.
Бинарные PDF не трогаем (никакой нормализации переносов строк).
"""

import base64
import hashlib
import sys
import zlib
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

try:
    from cryptography.fernet import Fernet  # noqa: F401 (проверка зависимости)
except Exception:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Ошибка",
        "Не установлен модуль cryptography.\n\n"
        "Установите его командой:\n"
        "pip install cryptography"
    )
    sys.exit(1)

# ----------------------------------------------------------------------------
# Единая расшифровка с клиентом (новый формат + legacy).
# ----------------------------------------------------------------------------

try:
    from crypto_utils import decrypt_data_auto as decrypt_bytes
except Exception:
    try:
        from config import ENCRYPTION_KEY as _ENCRYPTION_KEY
    except Exception:
        _ENCRYPTION_KEY = b"legalyze-fernet-key-32bytes-long!"

    _KEY = base64.urlsafe_b64encode(hashlib.sha256(_ENCRYPTION_KEY).digest())
    _fernet = Fernet(_KEY)
    _LEGACY = Fernet(
        base64.urlsafe_b64encode(hashlib.sha256(b"LegalyzeSecretKey").digest())
    )

    def decrypt_bytes(data: bytes) -> bytes:  # type: ignore[no-redef]
        try:
            return _fernet.decrypt(data)
        except Exception:
            pass
        raw = _LEGACY.decrypt(data)
        try:
            return zlib.decompress(raw)
        except Exception:
            return raw


# ----------------------------------------------------------------------------
# Пост-обработка: PDF сохраняем байт-в-байт, текст — с нормализацией строк.
# ----------------------------------------------------------------------------

def postprocess_payload(raw: bytes) -> tuple[bytes, str]:
    """Возвращает (байты, расширение 'pdf' или 'txt')."""
    if not raw:
        return b"", "txt"
    if raw[:4] == b"%PDF":
        return raw, "pdf"  # бинарный PDF трогать нельзя
    try:
        text = raw.decode("utf-8")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text.encode("utf-8"), "txt"
    except Exception:
        return raw, "bin"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Legalyze Decrypt Tool")
        self.root.geometry("780x230")
        self.root.resizable(False, False)

        self.files = []
        self.dir_var = ""

        tk.Label(root, text="Выбранные файлы (.enc):").grid(
            row=0,
            column=0,
            sticky="w",
            padx=10,
            pady=5
        )

        self.file_entry = tk.Entry(root, width=72)
        self.file_entry.grid(row=0, column=1, padx=5, pady=5)

        tk.Button(
            root,
            text="Выбрать файлы",
            command=self.choose_files
        ).grid(row=0, column=2, padx=5, pady=5)

        tk.Label(root, text="Папка сохранения:").grid(
            row=1,
            column=0,
            sticky="w",
            padx=10,
            pady=5
        )

        self.dir_entry = tk.Entry(root, width=72)
        self.dir_entry.grid(row=1, column=1, padx=5, pady=5)

        tk.Button(
            root,
            text="Выбрать папку",
            command=self.choose_dir
        ).grid(row=1, column=2, padx=5, pady=5)

        tk.Button(
            root,
            text="Расшифровать",
            command=self.decrypt
        ).grid(row=2, column=1, pady=20)

    def choose_files(self):
        paths = filedialog.askopenfilenames(
            title="Выберите зашифрованные файлы (.enc)",
            filetypes=[
                ("Encrypted files", "*.enc"),
                ("All files", "*.*")
            ]
        )

        if not paths:
            return

        self.files = [Path(p) for p in paths]

        self.file_entry.delete(0, tk.END)

        if len(self.files) == 1:
            self.file_entry.insert(0, str(self.files[0]))
        else:
            self.file_entry.insert(0, f"Выбрано файлов: {len(self.files)}")

        if not self.dir_var:
            try:
                default_dir = str(self.files[0].parent)
                self.dir_var = default_dir
                self.dir_entry.insert(0, default_dir)
            except Exception:
                pass

    def choose_dir(self):
        path = filedialog.askdirectory(title="Выберите папку сохранения")

        if not path:
            return

        self.dir_var = path
        self.dir_entry.delete(0, tk.END)
        self.dir_entry.insert(0, path)

    def decrypt(self):
        if not self.files:
            messagebox.showerror("Ошибка", "Не выбраны входные файлы")
            return

        if not self.dir_var.strip():
            messagebox.showerror("Ошибка", "Не выбрана папка сохранения")
            return

        output_dir = Path(self.dir_var.strip())

        if not output_dir.is_dir():
            messagebox.showerror("Ошибка", "Папка сохранения не существует")
            return

        ok = []
        errors = []

        for input_path in self.files:
            try:
                if not input_path.is_file():
                    errors.append(f"{input_path.name}: файл не найден")
                    continue

                encrypted_data = input_path.read_bytes()

                # Расшифровка (новый формат или legacy)
                decrypted_raw = decrypt_bytes(encrypted_data)

                # Авто-выбор расширения: PDF -> .pdf, текст -> .txt
                final_payload, ext = postprocess_payload(decrypted_raw)
                output_path = output_dir / (input_path.stem + f".{ext}")

                output_path.write_bytes(final_payload)
                ok.append(str(output_path))

            except Exception as exc:
                errors.append(f"{input_path.name}: Неверный ключ или поврежденный файл ({exc})")

        if ok and not errors:
            messagebox.showinfo(
                "Готово",
                "Расшифрованные файлы сохранены:\n\n" + "\n".join(ok)
            )
        elif ok and errors:
            messagebox.showwarning(
                "Часть файлов расшифрована",
                "Успешно:\n" + "\n".join(ok) +
                "\n\nОшибки:\n" + "\n".join(errors)
            )
        else:
            messagebox.showerror(
                "Ошибка",
                "\n".join(errors) or "Неизвестная ошибка"
            )


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
