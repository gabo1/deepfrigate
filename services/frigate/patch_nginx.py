"""Add authenticated DeepFrigate API routing to Frigate's stock nginx."""

from pathlib import Path

CONFIG_PATH = Path("/usr/local/nginx/conf/nginx.conf")
MARKER = "# DeepFrigate authenticated API"

UPSTREAM = """    # DeepFrigate authenticated API
    upstream deepfrigate_platform {
        server platform-api:8080;
        keepalive 32;
    }

"""

ROUTE = """        # DeepFrigate authenticated API
        location ^~ /api/deepfrigate/ {
            include auth_request.conf;
            rewrite ^/api/deepfrigate/(.*)$ /$1 break;
            proxy_pass http://deepfrigate_platform;
            include proxy.conf;
            proxy_cache off;
            add_header Cache-Control "no-store";
            expires off;
        }

"""


def patch_nginx(text: str) -> str:
    if MARKER in text:
        return text

    upstream_anchor = "    upstream mqtt_ws {"
    route_anchor = "        location /api/ {"
    if upstream_anchor not in text or route_anchor not in text:
        raise RuntimeError("Unsupported Frigate nginx.conf layout")

    text = text.replace(upstream_anchor, UPSTREAM + upstream_anchor, 1)
    return text.replace(route_anchor, ROUTE + route_anchor, 1)


if __name__ == "__main__":
    CONFIG_PATH.write_text(
        patch_nginx(CONFIG_PATH.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
