#!/usr/bin/env python3
"""
Извлекает список моделей телефонов (<model ua="...">Имя</model>) из
шаблона провижининга в формате 3CX (*.ph.xml) и сохраняет в JSON.

Используется как первый шаг при добавлении нового вендора в проект:
1. Найти публичный .ph.xml для нужного вендора (например,
   github.com/jonhusen/3cx-provisioning-templates)
2. python3 extract_3cx_models.py <файл.ph.xml> <вендор>_models.json
3. Полученный список моделей — основа для заполнения таблицы
   phone_models (см. schema.sql) при добавлении вендора.

Файл конфигурации (%%переменные%%, блок <deviceconfig>) этот скрипт
не трогает — синтаксис 3CX (%%var%%, {IF ua=...}) достаточно сильно
отличается от Jinja2, чтобы конвертировать его автоматически надёжно;
перенос полей в шаблон делается вручную, по образцу того, как это
сделано в fanvil.xml.j2 / yealink.cfg.j2 (см. phone_vendors в
schema.sql) — берутся только реально нужные проекту поля (SIP-регистрация,
NTP, часовой пояс, язык, пароль администратора), а не весь объём
настроек оригинала (DECT/мультикаст/логотипы и т.п. в 3CX-шаблонах
обычно не нужны и только усложняют перенос).
"""
import json
import re
import sys


def extract_models(path):
    # 3CX-файлы часто в utf-8, но некоторые — в cp1252/latin-1;
    # errors="replace" не даёт упасть на редких проблемных байтах
    # (только в служебных полях вроде logo/interfaceLink, на список
    # моделей это не влияет).
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    return re.findall(r'<model ua="([^"]+)"[^>]*>([^<]+)</model>', content)


def main():
    if len(sys.argv) != 3:
        print(f"Использование: {sys.argv[0]} <файл.ph.xml> <выходной.json>")
        sys.exit(1)

    src, dst = sys.argv[1], sys.argv[2]
    models = extract_models(src)
    if not models:
        print("Моделей не найдено — проверьте, что это реальный .ph.xml файл 3CX")
        sys.exit(1)

    data = [{"model_key": ua, "display_name": name} for ua, name in models]
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Найдено моделей: {len(data)}")
    print(f"Сохранено в {dst}")


if __name__ == "__main__":
    main()
