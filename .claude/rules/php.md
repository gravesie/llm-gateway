---
description: PHP is not installed on the dev PC, so run it in Docker and syntax-check every changed file; loaded when a PHP file is opened.
paths: ["**/*.php"]
---

# PHP

- PHP is not installed on the dev PC. Run PHP tools in Docker (Docker Desktop must be running), or leave the check to CI and say so. Never report PHP as linted or tested unless one of the two ran.
- Syntax: `php -l` on every PHP file you changed; the output must be clean. In a WordPress theme or plugin, a syntax error in an entry file (`functions.php`, the main plugin file) takes the whole site down.
- Tests: PHPUnit, in Docker, in a project that has a suite. Use the project's own script for it (its CLAUDE.md names it). A project with no PHP suite says so there, and each change is checked on the live site after deploy.
- Scripts, ports and live-site checks are project facts: they stay in the project's CLAUDE.md. This file holds only what is true of PHP on this PC.
