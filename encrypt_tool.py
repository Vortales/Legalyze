"""Legalyze Encrypt Tool — шифрование готового PDF-промта для загрузки на сервер.

ТЗ 1.3: администратор сам готовит .pdf промт и шифрует его этим инструментом.
Формат единый с клиентом (crypto_utils.encrypt_data): Fernet поверх сырых
байтов, без сжатия и без переделки содержимого — PDF шифруется как есть.
"""

import base64
import hashlib
from pathlib import Path
from tkinter import Tk, Label, Entry, Button, filedialog, messagebox

# Единый ключ/формат с клиентом. Fallback — на случай запуска без config.py.
try:
    from crypto_utils import encrypt_data as _encrypt_data
except Exception:
    try:
        from config import ENCRYPTION_KEY as _ENCRYPTION_KEY
    except Exception:
        _ENCRYPTION_KEY = b"legalyze-fernet-key-32bytes-long!"

    from cryptography.fernet import Fernet

    _FERNET = Fernet(
        base64.urlsafe_b64encode(hashlib.sha256(_ENCRYPTION_KEY).digest())
    )

    def _encrypt_data(data: bytes) -> bytes:  # type: ignore[no-redef]
        return _FERNET.encrypt(data)


def encrypt_bytes(data: bytes) -> bytes:
    return _encrypt_data(data)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Legalyze Encrypt Tool (PDF)")
        self.root.geometry("700x250")
        self.root.resizable(False, False)

        self.file_var = ""
        self.dir_var = ""

        Label(root, text="Выбранный файл:").grid(row=0, column=0, sticky="w", padx=10, pady=5)
        self.file_entry = Entry(root, width=70)
        self.file_entry.grid(row=0, column=1, padx=5, pady=5)
        Button(root, text="Выбрать файл", command=self.choose_file).grid(row=0, column=2, padx=5, pady=5)

        Label(root, text="Папка сохранения:").grid(row=1, column=0, sticky="w", padx=10, pady=5)
        self.dir_entry = Entry(root, width=70)
        self.dir_entry.grid(row=1, column=1, padx=5, pady=5)
        Button(root, text="Выбрать папку", command=self.choose_dir).grid(row=1, column=2, padx=5, pady=5)

        hint = Label(
            root,
            text="Выберите готовый .pdf промт (или .txt) — он зашифруется как есть, без изменений.",
            fg="gray",
        )
        hint.grid(row=2, column=0, columnspan=3, padx=10, pady=2, sticky="w")

        Button(root, text="Зашифровать", command=self.encrypt).grid(row=3, column=1, pady=15)

    def choose_file(self):
        path = filedialog.askopenfilename(
            title="Выберите PDF файл промта",
            filetypes=[("PDF files", "*.pdf"), ("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.file_var = path
            self.file_entry.delete(0, "end")
            self.file_entry.insert(0, path)

    def choose_dir(self):
        path = filedialog.askdirectory(title="Выберите папку сохранения")
        if path:
            self.dir_var = path
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, path)

    def encrypt(self):
        input_path = Path(self.file_var)
        output_dir = Path(self.dir_var)

        if not input_path.is_file():
            messagebox.showerror("Ошибка", "Не выбран входной файл")
            return

        if not output_dir.is_dir():
            messagebox.showerror("Ошибка", "Не выбрана папка сохранения")
            return

        try:
            # Читаем файл как байты (PDF — бинарный, текст не трогаем вообще).
            raw = input_path.read_bytes()
            if not raw:
                messagebox.showerror("Ошибка", "Входной файл пуст")
                return

            encrypted = encrypt_bytes(raw)
            output_path = output_dir / (input_path.stem + ".enc")
            output_path.write_bytes(encrypted)
            messagebox.showinfo("Готово", f"Файл сохранен:\n{output_path}")
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))


if __name__ == "__main__":
    root = Tk()
    app = App(root)
    root.mainloop()
