import asyncio
import random
import ssl
import json
import time
import uuid
from loguru import logger
from websockets_proxy import Proxy, proxy_connect

# Constants
MAX_RETRIES = 5
MAX_CONNECTIONS = 10  # Use fewer connections for stability
PING_INTERVAL = 30   # Increased interval for sending PING in seconds (more frequent)
RETRY_DELAY = 10      # Delay in seconds for failed proxy retries
STABLE_PROXIES_THRESHOLD = 3  # Minimum successes for a proxy to be considered stable
CHECK_INTERVAL = 60    # Time in seconds to periodically check connections
MAX_FAIL_COUNT = 3     # Maximum number of failures before a proxy is marked unstable

# Track proxy performance and status
proxy_health = {}
active_proxies = set()
unstable_proxies = set()

async def connect_to_wss(proxy_url, user_id):
    device_id = str(uuid.uuid3(uuid.NAMESPACE_DNS, proxy_url))
    retry_count = 0
    successful_attempts = proxy_health.get(proxy_url, {}).get('success', 0)
    failure_attempts = proxy_health.get(proxy_url, {}).get('fail', 0)

    while retry_count < MAX_RETRIES:
        try:
            await asyncio.sleep(random.uniform(0.5, 1.5))  # Stabilizing delay before each connection attempt
            custom_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            }
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            uri = "wss://proxy2.wynd.network:4444/"

            # Create the Proxy instance
            proxy = Proxy.from_url(proxy_url)

            async with proxy_connect(uri, proxy=proxy, ssl=ssl_context, extra_headers={
                "Origin": "chrome-extension://lkbnfiajjmbhnfledhphioinpickokdi",
                "User-Agent": custom_headers["User-Agent"]
            }) as websocket:

                # Reset retry counts on successful connection
                retry_count = 0
                successful_attempts += 1
                proxy_health[proxy_url] = {'success': successful_attempts, 'fail': failure_attempts}
                active_proxies.add(proxy_url)
                unstable_proxies.discard(proxy_url)

                async def send_ping():
                    while True:
                        try:
                            send_message = json.dumps(
                                {"id": str(uuid.uuid4()), "version": "1.0.0", "action": "PING", "data": {}})
                            logger.debug(f"Sending PING with Device ID: {device_id}")
                            await websocket.send(send_message)
                            await asyncio.sleep(PING_INTERVAL)  # More frequent pings to maintain connection
                        except Exception as e:
                            logger.warning(f"Ping failed for proxy {proxy_url}: {e}")
                            break  # Attempt a reconnect on ping failure

                send_ping_task = asyncio.create_task(send_ping())
                try:
                    while True:
                        try:
                            response = await asyncio.wait_for(websocket.recv(), timeout=15)
                            message = json.loads(response)
                            logger.info(f"Received message: {message}")

                            if message.get("action") == "AUTH":
                                auth_response = {
                                    "id": message["id"],
                                    "origin_action": "AUTH",
                                    "result": {
                                        "browser_id": device_id,
                                        "user_id": user_id,
                                        "user_agent": custom_headers['User-Agent'],
                                        "timestamp": int(time.time()),
                                        "device_type": "extension",
                                        "version": "4.26.2",
                                        "extension_id": "lkbnfiajjmbhnfledhphioinpickokdi"
                                    }
                                }
                                logger.debug(f"Sending AUTH response: {auth_response}")
                                await websocket.send(json.dumps(auth_response))

                            elif message.get("action") == "PONG":
                                pong_response = {"id": message["id"], "origin_action": "PONG"}
                                logger.debug(f"Sending PONG response: {pong_response}")
                                await websocket.send(json.dumps(pong_response))

                        except (asyncio.TimeoutError, json.JSONDecodeError) as e:
                            logger.warning(f"Connection lost for proxy {proxy_url} due to: {e}")
                            break  # Exit loop to restart connection on timeout or JSON error

                        # Handle specific error messages from the server to determine proxy instability
                        if message.get("action") == "ERROR":
                            error_message = message.get("message", "")
                            if "blocked" in error_message or "rate-limited" in error_message:
                                logger.error(f"Proxy {proxy_url} received error: {error_message}")
                                proxy_health[proxy_url] = {'success': successful_attempts, 'fail': failure_attempts + 1}
                                unstable_proxies.add(proxy_url)
                                active_proxies.discard(proxy_url)
                                return None  # Mark proxy as unstable and return

                finally:
                    send_ping_task.cancel()
                    
        except (Exception, asyncio.TimeoutError) as e:
            retry_count += 1
            failure_attempts += 1
            proxy_health[proxy_url] = {'success': successful_attempts, 'fail': failure_attempts}
            logger.error(f"Error with proxy {proxy_url} (Attempt {retry_count}/{MAX_RETRIES}): {str(e)}")

            if retry_count >= MAX_RETRIES:
                logger.info(f"Reached max retries for proxy {proxy_url}. Moving to unstable list.")
                active_proxies.discard(proxy_url)
                unstable_proxies.add(proxy_url)
                return None  # Signal to the main loop to replace this proxy

            await asyncio.sleep(RETRY_DELAY)  # Delay before retrying to avoid rapid reconnections

async def main():
    _user_id = input("Enter your User ID: ").strip()
    proxy_file = 'proxy.txt'  # Path to your proxy file
    with open(proxy_file, 'r') as file:
        all_proxies = file.read().splitlines()

    active_list = random.sample(all_proxies, MAX_CONNECTIONS)
    tasks = {asyncio.create_task(connect_to_wss(proxy, _user_id)): proxy for proxy in active_list}

    while True:
        done, pending = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.result() is None:
                failed_proxy = tasks[task]
                logger.info(f"Removing and replacing failed proxy: {failed_proxy}")
                active_list.remove(failed_proxy)
                new_proxy = get_fallback_proxy(all_proxies)
                active_list.append(new_proxy)
                new_task = asyncio.create_task(connect_to_wss(new_proxy, _user_id))
                tasks[new_task] = new_proxy  # Replace the task in the dictionary
            tasks.pop(task)  # Remove the completed task whether it succeeded or failed
        await asyncio.sleep(CHECK_INTERVAL)  # Consistent interval to check connections
        # Replenish the tasks if any have completed
        for proxy in set(active_list) - set(tasks.values()):
            new_task = asyncio.create_task(connect_to_wss(proxy, _user_id))
            tasks[new_task] = proxy

def get_fallback_proxy(all_proxies):
    available_proxies = [proxy for proxy in all_proxies if proxy not in unstable_proxies]
    if available_proxies:
        return random.choice(available_proxies)
    return random.choice(all_proxies)  # Fallback to any proxy if no stable proxies are available

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Graceful shutdown initiated.")
