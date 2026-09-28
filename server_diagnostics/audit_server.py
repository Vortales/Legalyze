#!/usr/bin/env python3
"""
Legalyze Server Audit Script (Ubuntu / Debian VPS)
=================================================
Скрипт для комплексной диагностики и сбора сведений о состоянии сервера:
  - ОС, ядро, ресурсы (RAM, CPU, диск)
  - Сеть, фаервол (UFW, iptables) и слушающие порты
  - Nginx: статус, активные конфигурации, SSL-сертификаты Certbot
  - Серверные процессы: Node.js, PM2, Docker, PostgreSQL, systemd-сервисы
  - Структура проекта в /var/www/legalyze (файлы, .env, updates, .next)

Использование на сервере:
  python3 audit_server.py
"""

import os
import sys
import subprocess
import shutil
import platform
import datetime
from pathlib import Path


def run_cmd(cmd: str, timeout: int = 15) -> str:
    """Выполняет команду оболочки и возвращает вывод или ошибку."""
    try:
        res = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
        if out and err:
            return f"{out}\n[stderr]: {err}"
        return out or err or "(нет вывода / пусто)"
    except subprocess.TimeoutExpired:
        return f"[ТАЙМАУТ {timeout}с]: {cmd}"
    except Exception as e:
        return f"[ОШИБКА]: {e}"


def mask_sensitive(text: str) -> str:
    """Маскирует пароли и секретные ключи в строках конфигураций."""
    lines = []
    for line in text.splitlines():
        lower = line.lower()
        if any(k in lower for k in ["password", "secret", "token", "database_url", "postgres://", "mysql://"]):
            if "=" in line:
                key, _ = line.split("=", 1)
                lines.append(f"{key}=***[СКРЫТО_В_ЦЕЛЯХ_БЕЗОПАСНОСТИ]***")
            elif ":" in line:
                key, _ = line.split(":", 1)
                lines.append(f"{key}: ***[СКРЫТО_В_ЦЕЛЯХ_БЕЗОПАСНОСТИ]***")
            else:
                lines.append("***[СКРЫТО_В_ЦЕЛЯХ_БЕЗОПАСНОСТИ]***")
        else:
            lines.append(line)
    return "\n".join(lines)


def section(title: str) -> str:
    bar = "=" * 80
    return f"\n{bar}\n# {title}\n{bar}\n"


def main():
    report = []
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    report.append(section(f"ОТЧЕТ АУДИТА СЕРВЕРА LEGALYZE ({now_str})"))

    # 1. СИСТЕМНАЯ ИНФОРМАЦИЯ
    report.append(section("1. СИСТЕМА И ЯДРО"))
    report.append("OS / Release:")
    report.append(run_cmd("cat /etc/os-release | grep -E 'PRETTY_NAME|VERSION_ID|ID=' || lsb_release -a"))
    report.append("\nKernel & Uptime:")
    report.append(run_cmd("uname -a; uptime"))
    report.append("\nПамять (RAM):")
    report.append(run_cmd("free -h"))
    report.append("\nДисковое пространство:")
    report.append(run_cmd("df -h / /var /var/www 2>/dev/null || df -h"))

    # 2. СЕТЬ И ПОРТЫ
    report.append(section("2. СЕТЕВЫЕ ПОРТЫ И ФАЕРВОЛ"))
    report.append("Слушающие порты (TCP):")
    report.append(run_cmd("ss -tulpn | grep -E ':(80|443|3000|5432|3306|22) ' || netstat -tulpn 2>/dev/null"))
    report.append("\nСтатус UFW (Firewall):")
    report.append(run_cmd("ufw status verbose 2>/dev/null || echo 'UFW не установлен'"))

    # 3. NGINX
    report.append(section("3. СОСТОЯНИЕ И КОНФИГУРАЦИЯ NGINX"))
    report.append("Статус службы Nginx:")
    report.append(run_cmd("systemctl status nginx --no-pager -l || which nginx"))
    report.append("\nТест синтаксиса Nginx (nginx -t):")
    report.append(run_cmd("nginx -t 2>&1"))

    report.append("\nАктивные конфигурации Nginx (/etc/nginx/sites-enabled/):")
    sites_enabled = Path("/etc/nginx/sites-enabled")
    if sites_enabled.exists():
        files = list(sites_enabled.iterdir())
        if files:
            for f in files:
                report.append(f"\n--- [Файл: {f}] ---")
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    report.append(content)
                except Exception as e:
                    report.append(f"Ошибка чтения: {e}")
        else:
            report.append("(директория sites-enabled пуста)")
    else:
        report.append("(/etc/nginx/sites-enabled не найдена)")

    conf_d = Path("/etc/nginx/conf.d")
    if conf_d.exists():
        c_files = list(conf_d.glob("*.conf"))
        if c_files:
            report.append("\nКонфигурации в /etc/nginx/conf.d/:")
            for f in c_files:
                report.append(f"\n--- [Файл: {f}] ---")
                try:
                    report.append(f.read_text(encoding="utf-8", errors="replace"))
                except Exception as e:
                    report.append(f"Ошибка чтения: {e}")

    # 4. SSL СЕРТИФИКАТЫ
    report.append(section("4. SSL СЕРТИФИКАТЫ (CERTBOT / LET'S ENCRYPT)"))
    report.append("Certbot сертификаты:")
    report.append(run_cmd("certbot certificates 2>&1 || which certbot || echo 'Certbot не установлен'"))
    report.append("\nПапка /etc/letsencrypt/live/:")
    report.append(run_cmd("ls -la /etc/letsencrypt/live/ 2>/dev/null || echo 'Папка live отсутствует'"))

    # 5. ПРОЦЕССЫ ПРИЛОЖЕНИЯ (NODE, PM2, SYSTEMD, DOCKER)
    report.append(section("5. ПРОЦЕССЫ ПРИЛОЖЕНИЯ (NODE / NEXT.JS / PM2 / DOCKER)"))
    report.append("Версии Node & NPM:")
    report.append(run_cmd("node -v 2>&1; npm -v 2>&1; pnpm -v 2>&1; yarn -v 2>&1"))

    report.append("\nСтатус PM2:")
    report.append(run_cmd("pm2 list 2>&1 || pm2 status 2>&1 || echo 'PM2 не запущен или не установлен'"))

    report.append("\nSystemd службы, связанные с legalyze / next / web:")
    report.append(run_cmd("systemctl list-units --type=service --state=running | grep -E 'legalyze|next|node|web|app|pm2' || echo 'Специфичных служб не найдено'"))

    report.append("\nЗапущенные процессы (ps aux):")
    report.append(run_cmd("ps aux | grep -E 'node|pm2|next|nginx|postgres|docker' | grep -v grep"))

    # 6. ДИАГНОСТИКА ПРОЕКТА В /var/www/legalyze
    report.append(section("6. ПРОЕКТ В /var/www/legalyze"))
    proj_dir = Path("/var/www/legalyze")
    if proj_dir.exists():
        report.append(f"Каталог проекта {proj_dir} найден:")
        report.append(run_cmd(f"ls -la {proj_dir}"))

        pkg_json = proj_dir / "package.json"
        if pkg_json.exists():
            report.append("\npackage.json (скрипты и зависимости):")
            try:
                report.append(pkg_json.read_text(encoding="utf-8", errors="replace"))
            except Exception as e:
                report.append(f"Ошибка чтения package.json: {e}")

        # Проверка .env файлов
        for env_name in [".env", ".env.local", ".env.production"]:
            env_file = proj_dir / env_name
            if env_file.exists():
                report.append(f"\nПеременные {env_name} (значения секретов замаскированы):")
                try:
                    raw = env_file.read_text(encoding="utf-8", errors="replace")
                    report.append(mask_sensitive(raw))
                except Exception as e:
                    report.append(f"Ошибка чтения {env_name}: {e}")

        # Проверка сборки .next
        next_dir = proj_dir / ".next"
        if next_dir.exists():
            report.append(f"\nПапка .next присутствует (размер: {run_cmd(f'du -sh {next_dir}')})")
        else:
            report.append("\nПапка .next ОТСУТСТВУЕТ (проект не собран через npm run build)")

        # Проверка директории обновлений
        updates_dir = proj_dir / "data" / "updates"
        if updates_dir.exists():
            report.append(f"\nФайлы обновлений в {updates_dir}:")
            report.append(run_cmd(f"ls -lah {updates_dir}"))
        else:
            report.append(f"\nДиректория обновлений {updates_dir} не создана")

    else:
        report.append(f"[ВНИМАНИЕ]: Каталог {proj_dir} не найден!")
        report.append("Проверка содержимого /var/www/:")
        report.append(run_cmd("ls -la /var/www/ 2>/dev/null || echo '/var/www не существует'"))

    # 7. ЛОГИ ОШИБОК NGINX И ПРИЛОЖЕНИЯ
    report.append(section("7. ПОСЛЕДНИЕ ЛОГИ ОШИБОК NGINX"))
    report.append("Последние 25 строк /var/log/nginx/error.log:")
    report.append(run_cmd("tail -n 25 /var/log/nginx/error.log 2>/dev/null || echo 'Лог пуст или отсутствует'"))

    # Финал
    full_output = "\n".join(report)
    print(full_output)

    # Сохраняем результат в файл рядом
    out_file = Path("server_audit_report.txt")
    try:
        out_file.write_text(full_output, encoding="utf-8")
        print(f"\n[+] Полный отчет сохранен в файл: {out_file.resolve()}\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
