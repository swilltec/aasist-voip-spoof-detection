#!/bin/sh
set -eu
envsubst '${POSTGRES_PASSWORD}' < /etc/asterisk/res_odbc.conf.template > /etc/asterisk/res_odbc.conf
envsubst '${SIP_PASSWORD} ${LAN_IP}' < /etc/asterisk/pjsip.conf.template > /etc/asterisk/pjsip.conf
export ASTERISK_IP="$(hostname -i | awk '{print $1}')"
envsubst '${ASTERISK_IP} ${LAN_IP}' < /etc/asterisk/rtp.conf.template > /etc/asterisk/rtp.conf
exec "$@"
