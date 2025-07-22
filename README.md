# 🛰️ Drone Agent Collaboration using LangGraph + MAVSDK + WebSockets

This project demonstrates a **multi-agent autonomous drone system** where two drones (Agent 1 and Agent 2) coordinate missions using natural language prompts, LangGraph-powered reasoning, and MAVSDK-based control. The drones communicate in real time via WebSockets and simulate tasks like counting people and cars.

---

## ✨ Features

- 🤖 **LangGraph-based ReAct agents** for autonomous reasoning
- 🚁 **MAVSDK integration** for drone control (connect, arm, takeoff)
- 🌐 **WebSocket-based peer-to-peer communication**
- 🧠 **LLM-driven delegation**: Drone 1 can delegate tasks to Drone 2
- 🔁 **Step-by-step ReAct traces** printed in both terminals
- ✅ **Final mission summary** after all sub-tasks are complete

---

## 🗺️ Architecture Overview

       ┌────────────┐                        ┌────────────┐
       │  Drone 1   │                        │  Drone 2   │
       │ (talk1.py) │                        │ (talk2.py) │
       └─────┬──────┘                        └─────┬──────┘
             │                                       │
     ┌───────▼────────┐                    ┌────────▼──────┐
     │ LangGraph Agent│                    │ LangGraph Agent│
     │  + LLM + Tools │                    │  + LLM + Tools │
     └───────┬────────┘                    └────────┬──────┘
             │                                       │
 ┌───────────▼────────────┐               ┌─────────▼──────────────┐
 │ WebSocket Client/Server│◄─────────────►│ WebSocket Client/Server│
 └────────────────────────┘               └────────────────────────┘


---

## 🚀 How to Run

### 1. Install dependencies

```bash
pip install -r requirements.txt
python talk2.py



USER ➜ please count people and cars

🤖 Drone 1 AI:
- Took off and counted 4 cars.
- Delegated people-counting to Drone 2.

🤖 Drone 2 AI:
- Connected, armed, took off.
- Counted 7 people and sent result to Drone 1.

📥 Drone 1 received: [Drone 2] Detected 7 people.

✅ Final Answer: There are 7 people and 4 cars.


.
├── talk1.py           # Drone 1 agent (ReAct planner + WebSocket)
├── talk2.py           # Drone 2 agent (ReAct planner + WebSocket)
├── requirements.txt   # Python dependencies
└── README.md          # Project documentation
