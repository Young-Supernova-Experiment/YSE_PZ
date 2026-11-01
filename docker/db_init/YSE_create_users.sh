#!/bin/bash
# Creates the application and read-only Explorer MySQL users for the docker stack.
# Passwords come from the YSE_DATABASE_PASSWORD and YSE_EXPLORER_PASSWORD environment
# variables (docker/.env); nothing is stored in the repository.
set -euo pipefail
: "${YSE_DATABASE_PASSWORD:?YSE_DATABASE_PASSWORD is not set}"
: "${YSE_EXPLORER_PASSWORD:?YSE_EXPLORER_PASSWORD is not set}"

esc() { printf '%s' "$1" | sed -e "s/\\\\/\\\\\\\\/g" -e "s/'/\\\\'/g"; }
APP_PW="$(esc "$YSE_DATABASE_PASSWORD")"
EXP_PW="$(esc "$YSE_EXPLORER_PASSWORD")"

mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --protocol=socket <<SQL
CREATE USER 'dev'@'%' IDENTIFIED WITH mysql_native_password BY '${APP_PW}';
GRANT ALL PRIVILEGES ON YSE.* TO 'dev'@'%' WITH GRANT OPTION;
GRANT CREATE, DROP ON *.* TO 'dev'@'%';
GRANT ALL PRIVILEGES ON \`test_YSE\`.* TO 'dev'@'%';

CREATE USER 'dev'@'localhost' IDENTIFIED WITH mysql_native_password BY '${APP_PW}';
GRANT ALL PRIVILEGES ON YSE.* TO 'dev'@'localhost' WITH GRANT OPTION;
GRANT CREATE, DROP ON *.* TO 'dev'@'localhost';
GRANT ALL PRIVILEGES ON \`test_YSE\`.* TO 'dev'@'localhost';

CREATE USER 'dev_explorer'@'%' IDENTIFIED WITH mysql_native_password BY '${EXP_PW}';
GRANT SELECT ON YSE.* TO 'dev_explorer'@'%';

CREATE USER 'dev_explorer'@'localhost' IDENTIFIED WITH mysql_native_password BY '${EXP_PW}';
GRANT SELECT ON YSE.* TO 'dev_explorer'@'localhost';

FLUSH PRIVILEGES;
SQL
