# LAMPP Panel

A small Tk control panel for a local web development stack on Fedora. It manages the
distribution's own services through systemd - **no XAMPP required**:

| Row      | Fedora package(s)           | Unit              |
|----------|-----------------------------|-------------------|
| Apache   | `httpd`                     | `httpd.service`   |
| MariaDB  | `mariadb-server`            | `mariadb.service` |
| PHP-FPM  | `php-fpm`, `php-mysqlnd`    | `php-fpm.service` |
| FTP      | `vsftpd`                    | `vsftpd.service`  |
| Postfix  | `postfix`                   | `postfix.service` |
| Tomcat   | `tomcat`                    | `tomcat.service`  |

Start/stop/restart, enable at boot, install missing services, start/stop the whole LAMP
stack, project launcher, log and config viewers, database backup (`mariadb-dump`).
phpMyAdmin (`phpMyAdmin` package) is opened by the MariaDB row's Admin button.

Not affiliated with Apache Friends or XAMPP.

## Install (Fedora)
    sudo dnf install lampp-panel        # pulls in httpd, mariadb-server, php-fpm, phpMyAdmin
    lampp-panel

## Projects folder
Apache serves `/var/www/html`, which is owned by root. Click **Grant Access** once to give
your user read/write access through an ACL (no ownership change, SELinux labels stay intact).

## Security design
- The GUI runs as a normal user and refuses to run as root.
- Root actions go through `pkexec /usr/libexec/lampp-panel-helper`, which accepts only a
  fixed whitelist of actions, services, packages and log files.
- No shell strings are built from file names or user input.
