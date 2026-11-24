---
description: Python line endings on Windows; loaded when a Python file is opened.
paths: ["**/*.py"]
---

# Python

- On Windows, Python text writes emit CRLF. Pass `newline='\n'` or use `write_bytes`, so a committed file keeps LF.
