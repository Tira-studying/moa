# 1. Python 웹 서버와 Node 기반 명세서 판독기를 같은 이미지에 준비합니다.
FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
# 2. Chromium 샌드박스와 일반 사용자(moa)를 준비합니다. root로 앱을 실행하지 않습니다.
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && apt-get update \
 && apt-get install -y --no-install-recommends chromium chromium-sandbox ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home moa \
 && mkdir /data && chown moa:moa /data
WORKDIR /app
# 3. 의존성을 먼저 설치하면 코드만 수정했을 때 이 단계를 재사용할 수 있습니다.
COPY requirements.txt package.json package-lock.json ./
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then \
      export PIP_CERT=/run/secrets/build_ca NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; \
    fi; \
    pip install --no-cache-dir -r requirements.txt \
 && npm ci --ignore-scripts --cache /tmp/npm-cache && rm -rf /tmp/npm-cache
# 4. 실행에 필요한 코드·화면·검증 파일을 복사합니다. 비밀 파일과 실제 DB는 복사하지 않습니다.
COPY migrations.py preview_store.py card_performance.py features.py tax_deduction.py budgets.py subscriptions.py manual_entries.py receipts.py export_xlsx.py connection_setup.py bank_expenses.py codef_bank.py codef_card_auth.py codef_direct.py codef_sync.py codef_worker.py codef_card.py codef_connection.py sms_inbox.py gmail_inbox.py mail_worker.py app.py shared_management.py shared_spending.py personal_data.py collaboration_access.py categories.py finance.py auth.py secure_html.py secure_pdf.py diagnostic_structure.py manage.py ./
COPY scripts ./scripts
COPY static ./static
COPY templates ./templates
COPY tests ./tests
RUN chmod -R a+rX /app
# 5. DB는 외부 볼륨 /data에 두고, 임시 작업은 /tmp에서 수행합니다.
ENV CARD_DB=/data/cards.sqlite3 MOA_CHROMIUM=/usr/bin/chromium \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 XDG_CACHE_HOME=/tmp/moa-cache \
    XDG_CONFIG_HOME=/tmp/moa-config
# 6. Gunicorn이 Flask 앱(app.py의 app)을 실행합니다.
USER moa
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "--bind", "127.0.0.1:8000", "--workers", "1", "--threads", "4", "--timeout", "60", "--umask", "0077", "--forwarded-allow-ips", "127.0.0.1,::1", "app:app"]
