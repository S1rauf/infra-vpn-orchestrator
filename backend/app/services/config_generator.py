# FILE: backend/app/services/config_generator.py
import json
import urllib.parse
from config import settings

def generate_vless_link(node, uuid: str, remark: str) -> str:
    # Берем порт и SNI из объекта ноды (или дефолт)
    port = node.port or 443
    sni = node.sni_domain or "www.google.com"
    
    params = {
        "type": "tcp", "security": "reality", "pbk": settings.env.REALITY_PUBLIC_KEY,
        "fp": "chrome", "sni": sni, "sid": settings.env.REALITY_SHORT_ID,
        "spx": "/", "flow": "xtls-rprx-vision"
    }
    safe_remark = urllib.parse.quote(remark)
    return f"vless://{uuid}@{node.domain}:{port}?{urllib.parse.urlencode(params)}#{safe_remark}"

def generate_singbox_config(nodes: list, user_uuid: str):
    """
    Генерирует умный JSON-профиль для Hiddify / Sing-box / V2Box.
    Включает:
    1. Selector (Ручной выбор)
    2. URL-Test (Авто-выбор по пингу)
    3. Direct (Для РФ сайтов)
    4. Block (Для рекламы)
    """
    
    # 1. DNS (Безопасный + Локальный для РФ)
    dns = {
        "servers": [
            {"tag": "dns-remote", "address": "https://1.1.1.1/dns-query", "detour": "proxy"},
            {"tag": "dns-local", "address": "https://77.88.8.8/dns-query", "detour": "direct"}, # Yandex DNS
            {"tag": "dns-block", "address": "rcode://success"}
        ],
        "rules": [
            {"outbound": "any", "server": "dns-local"},
            {"clash_mode": "Direct", "server": "dns-local"},
            {"geosite": "ru", "server": "dns-local"},
            {"domain_suffix": [".ru", ".su", ".rf", ".moscow"], "server": "dns-local"}
        ],
        "strategy": "ipv4_only" # Для стабильности
    }

    # 2. Outbounds (Серверы)
    outbounds = []
    node_tags = []

    for i, node in enumerate(nodes):
        tag = f"🚀 {node.country_code} {node.name}"
        node_tags.append(tag)
        
        # Используем динамические настройки
        port = node.port or 443
        sni = node.sni_domain or "www.google.com"

        vless_out = {
            "type": "vless", "tag": tag, "server": node.domain, "server_port": port,
            "uuid": user_uuid, "flow": "xtls-rprx-vision",
            "tls": {
                "enabled": True, "server_name": sni,
                "utls": {"enabled": True, "fingerprint": "chrome"},
                "reality": {"enabled": True, "public_key": settings.env.REALITY_PUBLIC_KEY, "short_id": settings.env.REALITY_SHORT_ID}
            },
            "packet_encoding": "xudp"
        }
        outbounds.append(vless_out)

    # Группы выбора
    # Авто-выбор (URL Test)
    url_test = {
        "type": "urltest",
        "tag": "⚡️ Авто-выбор (Лучший пинг)",
        "outbounds": node_tags,
        "url": "https://www.gstatic.com/generate_204",
        "interval": "3m",
        "tolerance": 50
    }
    
    # Ручной выбор (Selector)
    selector = {
        "type": "selector",
        "tag": "proxy",
        "outbounds": ["⚡️ Авто-выбор (Лучший пинг)"] + node_tags + ["direct"],
        "default": "⚡️ Авто-выбор (Лучший пинг)"
    }

    outbounds.insert(0, selector)
    outbounds.insert(1, url_test)
    outbounds.append({"type": "direct", "tag": "direct"})
    outbounds.append({"type": "block", "tag": "block"})

    # 3. Маршрутизация (Routing)
    # Здесь настраиваем умные правила
    route = {
        "rules": [
            {"geosite": "category-ads-all", "outbound": "block"},
            {"geosite": "ru", "outbound": "direct"},
            {"geoip": "ru", "outbound": "direct"},
            {"domain_suffix": [".ru", ".su", ".rf", "gosuslugi.ru", "sberbank.ru", "tbank.ru"], "outbound": "direct"},
            {"clash_mode": "Direct", "outbound": "direct"},
            {"clash_mode": "Global", "outbound": "proxy"}
        ],
        "final": "proxy",
        "auto_detect_interface": True
    }

    config = {
        "log": {"level": "warn"},
        "dns": dns,
        "inbounds": [{"type": "tun", "interface_name": "tun0", "auto_route": True, "strict_route": True}],
        "outbounds": outbounds,
        "route": route
    }
    
    return json.dumps(config, indent=2)