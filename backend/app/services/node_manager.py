# ==============================================================================
# FILE: backend/app/services/node_manager.py
# ==============================================================================
import ansible_runner
import asyncio
import httpx
import os
import shutil
import logging
from CloudFlare import CloudFlare
from sqlalchemy import select, func
from database import async_session_maker
from models.server import ServerNode, ServerCluster # <--- Добавили Cluster
from config import settings
from utils.security import encrypt_password 

logger = logging.getLogger(__name__)

# ПУТИ
PLAYBOOK_SOURCE = "/app/ansible/setup_node.yml"
CERT_PATH = "/var/lib/marzban/certs/ca.pem" 
RUN_DIR = "/tmp/ansible_runtime"

async def deploy_new_server(ip: str, root_password: str):
    logs = [f"🚀 Начинаем деплой сервера {ip}..."]
    
    try: # <--- НАЧАЛО TRY
        # 1. ПОЛУЧАЕМ ВНЕШНИЙ IP ГЛАВНОГО СЕРВЕРА
        main_panel_ip = "0.0.0.0/0"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get("https://api.ipify.org", timeout=5)
                if resp.status_code == 200:
                    main_panel_ip = resp.text.strip()
        except Exception as e:
            logs.append(f"⚠️ Не удалось определить IP панели: {e}")

        # 2. ПОЛУЧАЕМ НАСТРОЙКИ REALITY
        from services.marzban_service import get_reality_settings_from_panel
        chosen_sni, chosen_port = await get_reality_settings_from_panel()
        logs.append(f"🎭 Маскировка: {chosen_sni}:{chosen_port}")

        # 3. ОПРЕДЕЛЯЕМ ГЕОПОЗИЦИЮ
        country, city = "UN", "Unknown"
        async with httpx.AsyncClient() as client:
            try: 
                geo_resp = await client.get(f"http://ip-api.com/json/{ip}", timeout=5.0)
                geo_data = geo_resp.json()
                country = geo_data.get('countryCode', 'UN')
                city = geo_data.get('city', 'Unknown')[:3].upper()
            except Exception as e:
                logs.append(f"⚠️ GeoIP Error: {e}")

        # 4. ГЕНЕРИРУЕМ ИМЯ И ДОМЕН
        async with async_session_maker() as db:
            stmt = select(func.count()).where(ServerNode.country_code == country)
            count = (await db.scalar(stmt)) + 1
            node_name = f"{country}-{city}-{count:02d}"
            
            domain_root = settings.env.MAIN_DOMAIN
            if settings.env.IS_TEST_ENV:
                domain_root = f"test.{settings.env.MAIN_DOMAIN}"
                
            domain = f"{country.lower()}-{count:02d}.{domain_root}"

        # 5. НАСТРОЙКА DNS (Cloudflare)
        try:
            cf = CloudFlare(token=settings.env.CLOUDFLARE_API_TOKEN)
            zones = cf.zones.get(params={'name': settings.env.MAIN_DOMAIN})
            if not zones: raise Exception(f"Zone {settings.env.MAIN_DOMAIN} not found")
            
            zone_id = zones[0]['id']
            dns_record = {'name': domain, 'type': 'A', 'content': ip, 'proxied': False}
            cf.zones.dns_records.post(zone_id, data=dns_record)
            logs.append(f"✅ DNS: {domain}")
            
            logs.append("⏳ Ждем 30 сек (DNS propagation)...")
            await asyncio.sleep(30)
            
        except Exception as e:
            logs.append(f"⚠️ Cloudflare Error: {e}")

        # 6. ПОДГОТОВКА ANSIBLE
        project_dir = os.path.join(RUN_DIR, "project")
        if os.path.exists(RUN_DIR): shutil.rmtree(RUN_DIR)
        os.makedirs(project_dir, exist_ok=True)
        
        if os.path.exists(PLAYBOOK_SOURCE):
            shutil.copy2(PLAYBOOK_SOURCE, os.path.join(project_dir, "setup_node.yml"))
        else:
            return False, f"❌ Playbook not found: {PLAYBOOK_SOURCE}"

        # 7. ЧТЕНИЕ СЕРТИФИКАТА
        try:
            with open(CERT_PATH, "r") as f:
                ca_cert = f.read()
        except Exception as e:
            return False, f"❌ Cert Error ({CERT_PATH}): {e}"

        # 8. ЗАПУСК ANSIBLE
        logs.append(f"⚙️ Запуск установки...")
        
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(
            None, 
            run_ansible_sync, 
            ip, root_password, ca_cert, chosen_sni, chosen_port, main_panel_ip, domain 
        )

        if res.status != 'successful':
            return False, f"❌ Ansible Failed: {res.status}"

        logs.append("✅ Soft installed.")

        # 9. ДОБАВЛЕНИЕ В MARZBAN
        async with httpx.AsyncClient() as client:
            try:
                marz_url = f"{settings.env.MARZBAN_API_URL}/api/node"
                headers = {'Authorization': f'Bearer {settings.env.MARZBAN_API_TOKEN}'}
                node_data = {
                    "name": node_name,
                    "address": domain,
                    "port": 62050,
                    "api_port": 62051,
                    "usage_coefficient": 1.0
                }
                m_resp = await client.post(marz_url, headers=headers, json=node_data, timeout=10.0)
                m_resp.raise_for_status()
                logs.append("✅ Node linked to Panel.")
            except Exception as e:
                logs.append(f"⚠️ Marzban API Error: {e}")

        # 10. СОХРАНЕНИЕ В БД И АВТО-ГРУППИРОВКА
        async with async_session_maker() as db:
            new_node = ServerNode(
                name=node_name,
                ip_address=ip,
                domain=domain,
                country_code=country,
                is_active=True,
                sni_domain=chosen_sni,
                port=443, 
                encrypted_password=encrypt_password(root_password) 
            )
            db.add(new_node)
            await db.flush() 

            # Ищем группу
            logs.append("🧩 Авто-распределение...")
            stmt = select(ServerCluster).where((ServerCluster.node_a_id == None) | (ServerCluster.node_b_id == None))
            target_cluster = await db.scalar(stmt)

            if target_cluster:
                if not target_cluster.node_a_id: target_cluster.node_a_id = new_node.id
                else: target_cluster.node_b_id = new_node.id
                new_node.cluster_id = target_cluster.id
                logs.append(f"✅ В группе: {target_cluster.name}")
            else:
                # Новая группа
                count = await db.scalar(select(func.count(ServerCluster.id)))
                new_cluster_name = f"Cluster-{count + 1}"
                new_cluster = ServerCluster(name=new_cluster_name, node_a_id=new_node.id)
                db.add(new_cluster)
                await db.flush()
                new_node.cluster_id = new_cluster.id
                logs.append(f"✅ Создана группа: {new_cluster_name}")

            await db.commit()
            
        logs.append(f"🎉 Готово! Сервер {node_name} работает.")
        return True, "\n".join(logs)

    except Exception as e: # <--- ВОТ ЭТОТ EXCEPT БЫЛ ПОТЕРЯН
        logger.exception("Deploy Error")
        return False, f"🔥 Fatal Error: {e}"

# --- ФУНКЦИЯ ЗАПУСКА ANSIBLE (Вне класса и try/except) ---
def run_ansible_sync(ip, pwd, cert, sni, port, main_ip, node_domain):
    inventory = {
        'all': {
            'hosts': {
                'new_node': {
                    'ansible_host': ip,
                    'ansible_user': 'root',
                    'ansible_ssh_pass': pwd,
                    'ansible_ssh_extra_args': '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
                }
            }
        }
    }

    return ansible_runner.run(
        private_data_dir=RUN_DIR,
        playbook='setup_node.yml',
        inventory=inventory,
        extravars={
            'panel_cert': cert,
            'reality_sni': sni,
            'reality_port': port,
            'main_panel_ip': main_ip,
            'node_domain': node_domain # Передаем домен!
        },
        quiet=True
    )

async def delete_server_infrastructure(node_name: str, domain: str):
    """
    Удаляет ноду из Marzban и DNS запись из Cloudflare.
    """
    # 1. Удаление из Marzban
    try:
        headers = {'Authorization': f'Bearer {settings.env.MARZBAN_API_TOKEN}'}
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.env.MARZBAN_API_URL}/api/nodes", headers=headers)
            if resp.status_code == 200:
                nodes = resp.json()
                node_id = next((n['id'] for n in nodes if n['name'] == node_name), None)
                
                if node_id:
                    await client.delete(f"{settings.env.MARZBAN_API_URL}/api/node/{node_id}", headers=headers)
                    logger.info(f"✅ Marzban Node deleted")
    except Exception as e:
        logger.error(f"Marzban cleanup error: {e}")

    # 2. Удаление из Cloudflare
    try:
        cf = CloudFlare(token=settings.env.CLOUDFLARE_API_TOKEN)
        # Получаем зоны, фильтруем по имени домена
        # Cloudflare API может вернуть несколько зон, если у вас их много
        # Лучше искать зону по имени MAIN_DOMAIN
        zones = cf.zones.get(params={'name': settings.env.MAIN_DOMAIN})
        
        if zones:
            zone_id = zones[0]['id']
            # Ищем запись A для конкретного поддомена
            dns_records = cf.zones.dns_records.get(zone_id, params={'name': domain})
            
            for record in dns_records:
                cf.zones.dns_records.delete(zone_id, record['id'])
                logger.info(f"✅ Cloudflare DNS deleted")
            
    except Exception as e:
        logger.error(f"Cloudflare cleanup error: {e}")

async def clean_temp_files():
    """Удаляет временные файлы Ansible"""
    temp_dir = "/tmp/ansible_runtime"
    try:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            print("🧹 Временные файлы Ansible очищены.")
    except Exception as e:
        print(f"⚠️ Ошибка очистки tmp: {e}")