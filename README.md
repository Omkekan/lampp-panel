# LAMPP Panel

A small Tk-based control panel for managing a local web development stack. It manages the distribution's native services through **systemd** — no XAMPP required.

> **Note:** LAMPP Panel is not affiliated with Apache Friends or XAMPP.

## Supported Distributions

LAMPP Panel is tailored for the **Fedora / RHEL family** and works out of the box on:

* Fedora (Workstation, KDE, Silverblue/Kinoite)
* Red Hat Enterprise Linux (RHEL)
* AlmaLinux
* Rocky Linux
* CentOS Stream
* Nobara Linux

> **Note:** Adapting LAMPP Panel for Debian, Ubuntu, or Arch Linux requires modifying the source code to replace `dnf` with `apt`/`pacman` and mapping Fedora-specific service names such as `httpd` to their local equivalents such as `apache2`.

## Managed Services

| Service     | Fedora Package(s)        | systemd Unit      |
| ----------- | ------------------------ | ----------------- |
| **Apache**  | `httpd`                  | `httpd.service`   |
| **MariaDB** | `mariadb-server`         | `mariadb.service` |
| **PHP-FPM** | `php-fpm`, `php-mysqlnd` | `php-fpm.service` |
| **FTP**     | `vsftpd`                 | `vsftpd.service`  |
| **Postfix** | `postfix`                | `postfix.service` |
| **Tomcat**  | `tomcat`                 | `tomcat.service`  |

## Features

* Start, stop, and restart individual services.
* Enable or disable services at boot.
* Install missing services and dependencies.
* Start or stop the entire LAMP stack.
* Launch projects from the control panel.
* View service logs.
* View service configuration files.
* Create database backups using `mariadb-dump`.
* Open **phpMyAdmin** using the **Admin** button in the MariaDB row.
* Grant user access to the Apache projects directory without changing ownership or SELinux labels.

## Installation

### Fedora / RHEL

Install the package using:

```bash
sudo dnf install lampp-panel
```

This installs the required components, including:

* `httpd`
* `mariadb-server`
* `php-fpm`
* `phpMyAdmin`

Then launch the application:

```bash
lampp-panel
```

## Projects Folder

Apache serves projects from:

```text
/var/www/html
```

By default, this directory is owned by `root`.

Click **Grant Access** once in LAMPP Panel to give your user read/write access through an **ACL**.

This approach:

* Does not change directory ownership.
* Preserves the existing permissions structure.
* Keeps SELinux labels intact.
* Allows you to work with projects without running the GUI as root.

## Security Design

LAMPP Panel is designed to keep the graphical application running with normal user privileges.

### Normal User Execution

The GUI:

* Runs as a regular user.
* Refuses to run as `root`.
* Does not require the entire application to run with elevated privileges.

### Privileged Operations

Operations requiring administrator privileges are delegated through:

```text
pkexec /usr/libexec/lampp-panel-helper
```

The helper accepts only a fixed whitelist of:

* Actions
* Services
* Packages
* Log files

### Input Handling

No shell command strings are constructed from filenames or arbitrary user input.

This limits the privileged helper to a controlled set of predefined operations and reduces the risk associated with executing commands with elevated privileges.

## License

Add your project's license information here.

## Disclaimer

LAMPP Panel is an independent project and is **not affiliated with, endorsed by, or sponsored by Apache Friends or XAMPP**.
