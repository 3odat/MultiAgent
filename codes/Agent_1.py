#!/usr/bin/env python3
"""
talk1.py – Drone 1 autonomous agent (LangGraph + WebSocket)
Run:  python talk1.py
Then paste a prompt like:
    Count cars and ask Drone 2 to count people.
Console will show the full ReAct trace.
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
from mavsdk import System   # comment this line if you’re testing without MAVSDK

# ── 1. MAVSDK Tools (Drone 1) ──────────────────────────────────────
DRONE1_SITL_ADDRESS = "udp://:14540"
DRONE1_GRPC_PORT    = 50051
_drone1 = None                      # global connection

@tool
async def connect() -> str:
    """Connect to Drone 1."""
    global _drone1
    if _drone1:
        return "Drone 1 already connected."
    _drone1 = System(port=DRONE1_GRPC_PORT)
    await _drone1.connect(system_address=DRONE1_SITL_ADDRESS)
    async for s in _drone1.core.connection_state():
        if s.is_connected:
            return "✅ Drone 1 connected."
    return "❌ Connection failed."

@tool
async def arm() -> str:
    """ Arm the drone """
    if not _drone1:
        return "❌ Not connected."
    await _drone1.action.arm()
    return "✅ Armed."


@tool
async def takeoff(altitude_m: float = 5.0) -> str:
    """Take off to the specified altitude (default 5 m)."""
    if not _drone1:
        return "❌ Not connected."
    await _drone1.action.set_takeoff_altitude(altitude_m)
    await _drone1.action.takeoff()
    await asyncio.sleep(8)
    return f"🛫 Took off to {altitude_m:.1f} m."

@tool
async def count_cars() -> str:
    """ count number of cars """
    cars = 2   # ← replace with real detector later
    return f"🚗 Detected {cars} cars."

# ── 2. WebSocket client+server to talk to Drone 2 ───────────────────
DRONE2_WS_URL = "ws://127.0.0.1:8002/ws"   # change IP if remote
outgoing_ws = None                         # set after connect()

@tool
async def send_peer_message(message: str) -> str:
    """Send text to Drone 2 over WebSocket, retrying up to 3 times on failure."""
    if outgoing_ws is None:
        return "❌ Cannot send: WebSocket to Drone 2 not connected."

    payload = {"sender": "drone1", "message": message}
    for attempt in range(1, 4):
        try:
            await outgoing_ws.send(json.dumps(payload))
            # console log for clarity
            print(f"📤 [Drone 1 → Drone 2] attempt {attempt}: {message}")
            return f"📤 Sent to Drone 2: {message}"
        except Exception as e:
            print(f"⚠️ Send attempt {attempt} failed: {e}")
            await asyncio.sleep(1)

    return f"❌ Failed to send after 3 attempts: {message}"


# ── 3. LangGraph agent setup ────────────────────────────────────────
tools = [send_peer_message, connect, arm, takeoff, count_cars]
llm   = ChatOpenAI(model="gpt-4o", temperature=0)
llm_with_tools = llm.bind_tools(tools)

SYS_MSG = SystemMessage(content=(
    "You are Drone 1’s autonomous control agent.\n\n"
    "Your mission is to plan and execute tasks needed to complete the user’s prompt.\n"
    "You have these tools: connect, arm, takeoff, count_cars, send_peer_message.\n"
    "Drone 2 is your teammate. It can perform: connect, arm, takeoff, count_people.\n\n"

    "If the user prompt involves people, you must instruct Drone 2 to handle that.\n"
    "Use `send_peer_message` to ask Drone 2 to do its task, then WAIT for its response.\n"
    "Drone 2 will send back its result via WebSocket. Treat it as a HumanMessage and continue reasoning.\n\n"

    "You must ALWAYS follow this sequence:\n"
    "1. connect\n"
    "2. arm\n"
    "3. takeoff\n"
    "4. perform mission tools (e.g. count_cars or delegate to Drone 2)\n\n"

    "Use the ReAct format: Thought → Action → Observation. Only say 'Final Answer' once:\n"
    "- You have completed ALL local tasks\n"
    "- You have received and understood Drone 2's response if you delegated a task.\n\n"

    "You MUST clearly narrate your reasoning and tool usage at each step."
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
drone1_graph = builder.compile()

# ── 4. FastAPI WebSocket server (port 8001) ─────────────────────────
#!/usr/bin/env python3
import asyncio, json, nest_asyncio, websockets, uvicorn
nest_asyncio.apply()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
from langgraph.graph import StateGraph, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition
from mavsdk import System
from termcolor import cprint

# ── Helpers for pretty logging ─────────────────────────────────────────
def log_ok(msg):    cprint(msg, "green")
def log_warn(msg):  cprint(msg, "yellow")
def log_err(msg):   cprint(msg, "red")

# ── WebSocket Client/Server Setup ─────────────────────────────────────
DRONE2_WS_URL = "ws://127.0.0.1:8002/ws"
outgoing_ws = None

app = FastAPI()
conversation = {"messages": []}
_drone1 = None
final_reported = False  # debounce for Final Answer

# ── 4. WebSocket Server: Receive from Drone 2 ─────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    log_ok("✅ Drone 1 WS server: peer connected.")
    try:
        while True:
            data = await ws.receive_text()
            parsed = json.loads(data)
            human_msg = f"[Drone 2] {parsed.get('message','')}"
            log_ok(f"📥 Received from Drone 2: {human_msg}")

            # Inject into LangGraph and re-run
            conversation["messages"].append(HumanMessage(content=human_msg))
            await run_graph_once()
    except WebSocketDisconnect:
        log_err("❌ Drone 1 WS disconnected.")

# ── 5. Shared State & Reasoning Helper ────────────────────────────────
async def run_graph_once():
    global final_reported
    new_state = await drone1_graph.ainvoke(conversation)
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

    log_ok(f"🤖 Drone 1 AI: {last_ai.content}")

    # Explicit notification when Drone 2 reply triggered the reasoning
    if len(conversation["messages"]) >= 2 and "[Drone 2]" in conversation["messages"][-2].content:
        log_warn("📨 Drone 1: Response from Drone 2 integrated into mission.")

# ── 6. Background task: Connect to Drone 2 WS ─────────────────────────
async def connect_to_drone2():
    global outgoing_ws
    while True:
        try:
            outgoing_ws = await websockets.connect(DRONE2_WS_URL)
            log_ok("🔌 Connected to Drone 2 WebSocket.")
            break
        except Exception:
            log_warn("⏳ Waiting for Drone 2 WS …")
            await asyncio.sleep(2)

# ── 7. Simple CLI Prompt Loop ────────────────────────────────────────
async def cli_loop():
    global final_reported
    while True:
        prompt = input("\nUSER ➜ ")
        if prompt.strip().lower() in {"exit", "quit"}:
            break

        # Reset final-answer flag for new mission
        final_reported = False
        conversation["messages"].clear()

        # User prompt enters the ReAct chain
        conversation["messages"].append(HumanMessage(content=prompt))
        await run_graph_once()

        # If the first response isn’t 'Final Answer', we may need to wait
        if not final_reported:
            log_warn("🕒 Waiting for response from Drone 2 (if needed)…")
            # spin until Final Answer appears
            while not final_reported:
                await asyncio.sleep(1)

# ── 8. Main Entrypoint ───────────────────────────────────────────────
if __name__ == "__main__":
    # start WS client + server + CLI concurrently
    async def main():
        loop = asyncio.get_event_loop()
        loop.create_task(connect_to_drone2())
        config = uvicorn.Config(app, host="0.0.0.0", port=8001,
                                log_level="critical")
        server = uvicorn.Server(config)
        loop.create_task(server.serve())
        await cli_loop()

    asyncio.run(main())

