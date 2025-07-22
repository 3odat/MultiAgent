#!/usr/bin/env python3
"""
talk2.py – Drone 2 autonomous agent (LangGraph + WebSocket)

• Exposes a WebSocket server on port 8002  →  /ws
• Opens a WebSocket client back to Drone 1’s /ws (port 8001)
• Runs a LangGraph ReAct agent able to:
      connect → arm → takeoff → count_people
      send_peer_message (reply to Drone 1)
"""

# ── 0. Imports ──────────────────────────────────────────────────────
import asyncio, json, nest_asyncio, websockets, uvicorn
nest_asyncio.apply()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
from langgraph.graph import StateGraph, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition
from mavsdk import System        # comment if running without MAVSDK

# ── 1. MAVSDK tools for Drone 2 ─────────────────────────────────────
DRONE2_SITL_ADDRESS = "udp://:14541"
DRONE2_GRPC_PORT    = 50052
_drone2 = None

@tool
async def connect() -> str:
    """Connect to Drone 2 via MAVSDK."""
    global _drone2
    if _drone2:
        return "Drone 2 already connected."
    _drone2 = System(port=DRONE2_GRPC_PORT)
    await _drone2.connect(system_address=DRONE2_SITL_ADDRESS)
    async for s in _drone2.core.connection_state():
        if s.is_connected:
            return "✅ Drone 2 connected."
    return "❌ Connection failed."

@tool
async def arm() -> str:
    """ Arm the drone """
    if not _drone2:
        return "❌ Not connected."
    await _drone2.action.arm()
    return "✅ Armed."

@tool
async def takeoff(altitude_m: float = 5.0) -> str:
    """Take off to the specified altitude (default 5 m)."""
    if not _drone2:
        return "❌ Not connected."
    await _drone2.action.set_takeoff_altitude(altitude_m)
    await _drone2.action.takeoff()
    await asyncio.sleep(8)
    return f"🛫 Took off to {altitude_m:.1f} m."


@tool
async def count_people() -> str:
    """ Count people number """
    people = 3                     # ← replace with real detector
    return f"🧍 Detected {people} people."

# ── 2. WebSocket client + server definitions ───────────────────────
DRONE1_WS_URL = "ws://127.0.0.1:8001/ws"    # adjust IP if remote
outgoing_ws = None                          # set once connected

from termcolor import cprint
import asyncio, json

def log2_ok(msg):    cprint(msg, "cyan")   # use a different color for Drone 2
def log2_warn(msg):  cprint(msg, "yellow")
def log2_err(msg):   cprint(msg, "red")

@tool
async def send_peer_message(message: str) -> str:
    """Send text back to Drone 1 over WebSocket, retrying up to 3 times."""
    if outgoing_ws is None:
        return "❌ Cannot send: WebSocket to Drone 1 not connected."

    payload = {"sender": "drone2", "message": message}
    for attempt in range(1, 4):
        try:
            await outgoing_ws.send(json.dumps(payload))
            log2_ok(f"📤 [Drone 2 → Drone 1] attempt {attempt}: {message}")
            return f"📤 Sent to Drone 1: {message}"
        except Exception as e:
            log2_warn(f"⚠️ Send attempt {attempt} failed: {e}")
            await asyncio.sleep(1)

    log2_err(f"❌ All retries failed sending to Drone 1: {message}")
    return f"❌ Failed to send after 3 attempts: {message}"


# ── 3. LangGraph agent setup ───────────────────────────────────────
tools = [send_peer_message, connect, arm, takeoff, count_people]
llm   = ChatOpenAI(model="gpt-4o", temperature=0)
llm_with_tools = llm.bind_tools(tools)

SYS_MSG = SystemMessage(content=(
    "You are Drone 2’s autonomous control agent.\n\n"
    "Your primary responsibility is to assist Drone 1 by executing delegated tasks, especially those involving counting people.\n\n"
    
    "You have these tools: connect, arm, takeoff, count_people, send_peer_message.\n"
    "Your actions MUST always begin with this sequence:\n"
    "1. connect\n"
    "2. arm\n"
    "3. takeoff to 5 meters dont forget the takeoff 5m your drone 1\n\n"

    "Once airborne, perform your assigned mission (e.g., count_people).\n"
    "Then, use `send_peer_message` to send the result BACK to Drone 1 via WebSocket.\n"
    "Only say 'Final Answer' AFTER you have:\n"
    "- Finished your mission\n"
    "- Sent the results to Drone 1\n"
    "- Clearly confirmed that the report was sent\n\n"

    "Use the ReAct format to think and act:\n"
    "Thought → Action → Observation.\n"
    "Narrate your process step-by-step.\n"
    "You are not a decision-maker; you are a trusted assistant executing precise instructions.\n"
))


async def planner(state: MessagesState) -> MessagesState:
    msgs: list[BaseMessage] = state.get("messages", [])
    if not msgs or msgs[0].type != "system":
        msgs = [SYS_MSG] + msgs
    result = await llm_with_tools.ainvoke(msgs)
    return {"messages": msgs + [result]}

builder = StateGraph(MessagesState)
builder.add_node("planner", planner)
builder.add_node("tools", ToolNode(tools))
builder.set_entry_point("planner")
builder.add_conditional_edges("planner", tools_condition)
builder.add_edge("tools", "planner")
drone2_graph = builder.compile()

# ── 4. FastAPI WebSocket server (port 8002) ─────────────────────────
# ── Imports & Logging Helpers ─────────────────────────────────────────
from termcolor import cprint
import asyncio, json, websockets, uvicorn

def log2_ok(msg):    cprint(msg, "cyan")
def log2_warn(msg):  cprint(msg, "yellow")
def log2_err(msg):   cprint(msg, "red")

# ── WebSocket & Conversation State ───────────────────────────────────
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from langchain_core.messages import HumanMessage
app = FastAPI()

conversation = {"messages": []}
final_reported = False  # ensure only one Final Answer per mission
outgoing_ws = None


DRONE1_WS_URL = "ws://127.0.0.1:8001/ws"

# ── 4. Graph Runner ──────────────────────────────────────────────────
async def run_graph_once():
    global final_reported
    new_state = await drone2_graph.ainvoke(conversation)
    conversation["messages"] = new_state["messages"]

    # Find last AI message
    last_ai = next((m for m in reversed(conversation["messages"]) if m.type=="ai"), None)
    if not last_ai:
        return

    # Debounce duplicate Final Answer
    if "Final Answer" in last_ai.content:
        if final_reported:
            return
        final_reported = True

    log2_ok(f"🤖 Drone 2 AI: {last_ai.content}")

# ── 5. WebSocket Server: Receive from Drone 1 ─────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    log2_ok("✅ Drone 2 WS server: peer connected.")
    try:
        while True:
            data = await ws.receive_text()
            parsed = json.loads(data)
            msg = parsed.get("message", "")
            log2_ok(f"📥 Received from Drone 1: {msg}")

            # Inject into LangGraph and re-run
            conversation["messages"].append(HumanMessage(content=f"[Drone 1] {msg}"))
            await run_graph_once()
    except WebSocketDisconnect:
        log2_err("❌ Drone 2 WS disconnected.")

# ── 6. Background Task: Connect to Drone 1’s WS ───────────────────────
async def connect_to_drone1():
    global outgoing_ws
    while True:
        try:
            outgoing_ws = await websockets.connect(DRONE1_WS_URL)
            log2_ok("🔌 Connected to Drone 1 WebSocket.")
            break
        except Exception:
            log2_warn("⏳ Waiting for Drone 1 WS …")
            await asyncio.sleep(2)

# ── 7. Main Entrypoint ────────────────────────────────────────────────
if __name__ == "__main__":
    async def main():
        loop = asyncio.get_event_loop()

        # Start WS client to Drone 1
        loop.create_task(connect_to_drone1())

        # Start FastAPI WS server for Drone 2
        config = uvicorn.Config(app, host="0.0.0.0", port=8002, log_level="info")
        server = uvicorn.Server(config)
        ws_task = asyncio.create_task(server.serve())
        log2_ok("🚀 Drone 2 WS server running at ws://0.0.0.0:8002/ws")

        # Keep running until interrupted
        try:
            await ws_task
        except KeyboardInterrupt:
            log2_warn("✋ Shutdown requested. Stopping server…")
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            log2_ok("✅ Drone 2 WS server stopped. Goodbye!")

    asyncio.run(main())
