# Ziggy Apache static aliases

Each stack has its own `STATIC_ROOT` under `/data/yse_pz/.../YSE_PZ/static/`.
Deploy runs `collectstatic` into that tree. Browsers only see those files if
Apache has a matching `Alias` **and** that stack’s `site_settings.ini`
`STATIC:` matches the Alias URL.

| Stack | App path | Alias (Apache) | `STATIC:` in settings.ini |
|-------|----------|----------------|---------------------------|
| production (`yse`) | `/data/yse_pz/YSE_PZ` | `/static/` | `/static/` |
| test (`yse_test`) | `/data/yse_pz/YSE_PZ_test` | `/test_static/` | `/test_static/` |
| experimental (`yse_experimental`) | `/data/yse_pz/YSE_PZ_experimental` | `/experimental_static/` | `/experimental_static/` |

## Add experimental static (one-time on Ziggy)

In `yse-experimental.conf` (sites-enabled), next to the other Alias/WSGI lines:

```apache
Alias /experimental_static/ /data/yse_pz/YSE_PZ_experimental/YSE_PZ/static/
<Directory /data/yse_pz/YSE_PZ_experimental/YSE_PZ/static>
    Require all granted
</Directory>
```

In `/data/yse_pz/YSE_PZ_experimental/YSE_PZ/settings.ini` (or whatever file
that install uses for `[site_settings]`):

```ini
STATIC: /experimental_static/
```

Then:

```bash
cd /data/yse_pz/YSE_PZ_experimental
./venv/bin/python manage.py collectstatic --noinput
sudo apache2ctl configtest && sudo systemctl reload apache2
```

Check:

```bash
curl -sI https://ziggy.ucolick.org/experimental_static/YSE_App/vendor/adminlte-3.2/css/adminlte.min.css
```

Expect `200`. Hard-refresh `yse_experimental`.

**Do not** point experimental `STATIC:` at `/static/` — that Alias is production
and will keep serving the old tree (or 404 new vendor paths).
