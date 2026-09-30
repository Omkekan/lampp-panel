Name:           lampp-panel
Version:        2.0.0
Release:        1%{?dist}
Summary:        Control panel for a local Apache, MariaDB and PHP stack
License:        MIT
URL:            https://github.com/YOURNAME/lampp-panel
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
Requires:       python3-tkinter
Requires:       python3-psutil
Requires:       polkit
Requires:       systemd
Requires:       acl
# The stack itself: installed by default, but the panel can also install any
# missing piece from its own window.
Recommends:     httpd
Recommends:     mariadb-server
Recommends:     php-fpm
Recommends:     php-mysqlnd
Recommends:     phpMyAdmin
Suggests:       vsftpd
Suggests:       postfix
Suggests:       tomcat

%description
A Tk control panel for a local web development stack on Fedora. It starts,
stops, restarts and monitors the distribution's own Apache (httpd), MariaDB,
PHP-FPM, vsftpd, Postfix and Tomcat services through systemd, can install any
that are missing, and includes a project launcher, log and config viewers and
database backup. Privileged actions run through a small whitelisted helper and
polkit. XAMPP is not required.

%prep
%autosetup

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files lamppanel
install -Dm644 data/lampp-panel.desktop %{buildroot}%{_datadir}/applications/lampp-panel.desktop
install -Dm644 data/lampp-panel.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/lampp-panel.svg
install -Dm644 data/io.github.YOURNAME.LamppPanel.metainfo.xml %{buildroot}%{_metainfodir}/io.github.YOURNAME.LamppPanel.metainfo.xml
install -Dm644 data/io.github.lamppanel.manage.policy %{buildroot}%{_datadir}/polkit-1/actions/io.github.lamppanel.manage.policy
install -Dm755 libexec/lampp-panel-helper %{buildroot}%{_libexecdir}/lampp-panel-helper

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/lampp-panel.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/*.metainfo.xml

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/lampp-panel
%{_datadir}/applications/lampp-panel.desktop
%{_datadir}/icons/hicolor/scalable/apps/lampp-panel.svg
%{_metainfodir}/*.metainfo.xml
%{_datadir}/polkit-1/actions/io.github.lamppanel.manage.policy
%{_libexecdir}/lampp-panel-helper

%changelog
* Wed Sep 30 2026 Your Name <you@example.com> - 2.0.0-1
- Standalone: manage Fedora's own httpd, mariadb, php-fpm, vsftpd, postfix and
  tomcat through systemd; XAMPP is no longer required
- Install missing services from the panel, start/stop the whole LAMP stack
- Read root-only logs through the helper, grant the user ACL access to /var/www/html
