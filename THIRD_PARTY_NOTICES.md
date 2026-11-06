# Third-party notices

FOCUS использует следующие открытые библиотеки. Полные тексты лицензий
находятся в папке `licenses/`.

## Runtime

- **CustomTkinter 6.0.0** — MIT License.
  Проект: <https://github.com/TomSchimansky/CustomTkinter>
  Текст лицензии: `licenses/customtkinter/LICENSE`.

- **pystray 0.19.5** — GNU Lesser General Public License v3 (LGPLv3).
  Проект: <https://github.com/moses-palmer/pystray>
  Тексты лицензий: `licenses/pystray/COPYING`, `licenses/pystray/COPYING.LGPL`.

- **Pillow 13.0.0** — MIT-CMU (HPND-подобная лицензия).
  Проект: <https://python-pillow.org/>
  Текст лицензии: `licenses/pillow/LICENSE`.

- **Python 3.14** — Python Software Foundation License.
  Текст лицензии: <https://docs.python.org/3/license.html>.

## Сборка

- **PyInstaller** — GPLv2 с исключением для сборки приложений.
  Проект: <https://pyinstaller.org/>
  Лицензия: <https://pyinstaller.org/en/stable/license.html>.

## Распространение EXE

`FocusTimer.exe` содержит Python и перечисленные runtime-библиотеки.
При публикации бинарного файла сохраняйте этот документ и папку `licenses/`
вместе с релизом. Исходный код приложения и инструкция по пересборке
находятся в этом же репозитории; это также позволяет заменить LGPL-компонент
(pystray) и собрать приложение заново.
