import time
import json
from locust import HttpUser, task, between
import websocket

class MigrationBotUser(HttpUser):
    # Wait between 2 and 5 seconds between tasks per user
    wait_time = between(2, 5)

    @task(3)
    def test_health_check(self):
        """Simulates checking system health."""
        self.client.get("/api/health")

    @task(1)
    def test_websocket_chat(self):
        """Simulates an active user chat session over WebSockets."""
        # Use a mock token for local testing
        token = "mock-ruhail.rizwan@tmcltd.com"
        # Extract host without http:// or https:// for websocket connection
        host_clean = self.host.replace("http://", "").replace("https://", "").rstrip("/")
        # Dynamically use secure WebSocket (wss://) if host is HTTPS
        protocol = "wss" if self.host.startswith("https") else "ws"
        ws_url = f"{protocol}://{host_clean}/ws?token={token}"
        
        start_time = time.time()
        try:
            # Establish WebSocket connection
            ws = websocket.create_connection(ws_url, timeout=5)
            
            # Receive initial connection confirmation
            init_resp = ws.recv()
            if not init_resp:
                raise Exception("Server closed connection without confirmation")
            
            init_data = json.loads(init_resp)
            if init_data.get("type") == "error":
                raise Exception(f"Server rejected WS: {init_data.get('message')}")
            
            # Send standard read overview request
            payload = {"content": "Show SD tracker overview"}
            ws.send(json.dumps(payload))
            
            # Consume the streamed response chunks
            while True:
                resp_raw = ws.recv()
                response = json.loads(resp_raw)
                # Stop when the agent finishes streaming
                if response.get("done") or response.get("type") == "error":
                    break
                
                # Safeguard against hanging tests (max 15s)
                if time.time() - start_time > 15:
                    break
            
            ws.close()
            # Record successful request inside Locust
            self.environment.events.request.fire(
                request_type="WebSocket",
                name="chat_flow",
                response_time=int((time.time() - start_time) * 1000),
                response_length=0,
                exception=None,
            )
        except Exception as e:
            # Record failure inside Locust statistics
            self.environment.events.request.fire(
                request_type="WebSocket",
                name="chat_flow",
                response_time=int((time.time() - start_time) * 1000),
                response_length=0,
                exception=e,
            )
