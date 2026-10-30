#!/bin/sh
# 컨테이너 최초 기동 시 1회 실행(데이터 볼륨이 비어 있을 때만). 앱 전용 비-슈퍼유저 역할과 개발/테스트 DB 를 만든다.
# 마이그레이션은 이 역할(DB 소유자)로 실행한다. btree_gist 는 PG13+ 에서 trusted 확장이라 슈퍼유저가 필요 없다.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v app_user="$APP_DB_USER" -v app_pw="$APP_DB_PASSWORD" <<'EOSQL'
CREATE ROLE :"app_user" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_pw';
CREATE DATABASE chungpamo OWNER :"app_user";
CREATE DATABASE chungpamo_test OWNER :"app_user";
REVOKE ALL ON DATABASE chungpamo FROM PUBLIC;
REVOKE ALL ON DATABASE chungpamo_test FROM PUBLIC;
EOSQL
