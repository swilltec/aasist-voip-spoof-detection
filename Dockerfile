FROM debian:bookworm-slim

ARG ASTERISK_VERSION=23.5.0
ARG ASTERISK_SHA256=8ed3a237e6d8dff94a09527a83143530b5afcc1aee73fa7b85c069bda507adac

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl pkg-config gettext-base \
    libedit-dev libjansson-dev libsqlite3-dev libssl-dev libxml2-dev \
    libncurses-dev libuuid1 uuid-dev unixodbc-dev odbc-postgresql libsrtp2-dev \
    && curl -fsSL "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.tar.gz" -o /tmp/asterisk.tar.gz \
    && echo "${ASTERISK_SHA256}  /tmp/asterisk.tar.gz" | sha256sum -c - \
    && mkdir /usr/src/asterisk \
    && tar -xzf /tmp/asterisk.tar.gz -C /usr/src/asterisk --strip-components=1 \
    && cd /usr/src/asterisk \
    && ./configure --with-pjproject-bundled --with-jansson-bundled=no \
    && make menuselect.makeopts \
    && menuselect/menuselect --disable BUILD_NATIVE --disable-category MENUSELECT_CORE_SOUNDS --disable-category MENUSELECT_MOH --disable-category MENUSELECT_EXTRA_SOUNDS menuselect.makeopts \
    && make -j2 && make install \
    && test -f /usr/lib/asterisk/modules/res_odbc.so \
    && test -f /usr/lib/asterisk/modules/cdr_adaptive_odbc.so \
    && test -f /usr/lib/asterisk/modules/res_srtp.so \
    && test -f /usr/lib/asterisk/modules/app_audiosocket.so \
    && test -f /usr/lib/asterisk/modules/res_audiosocket.so \
    && test -f /usr/lib/asterisk/modules/func_uuid.so \
    && test -f /usr/lib/asterisk/modules/func_cdr.so \
    && useradd --system --home-dir /var/lib/asterisk asterisk \
    && mkdir -p /etc/asterisk /var/run/asterisk /var/log/asterisk /var/spool/asterisk \
    && chown -R asterisk:asterisk /etc/asterisk /var/run/asterisk /var/log/asterisk /var/spool/asterisk /var/lib/asterisk \
    && rm -rf /usr/src/asterisk /tmp/asterisk.tar.gz /var/lib/apt/lists/*

COPY --chown=asterisk:asterisk asterisk/ /etc/asterisk/
COPY asterisk/pjsip.conf /etc/asterisk/pjsip.conf.template
COPY asterisk/res_odbc.conf /etc/asterisk/res_odbc.conf.template
COPY asterisk/rtp.conf /etc/asterisk/rtp.conf.template
COPY docker/odbc.ini /etc/odbc.ini
COPY docker/entrypoint.sh /usr/local/bin/asterisk-entrypoint

USER asterisk
ENTRYPOINT ["/bin/sh", "/usr/local/bin/asterisk-entrypoint"]
CMD ["asterisk", "-f", "-vvv"]
