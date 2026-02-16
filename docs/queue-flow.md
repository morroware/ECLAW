# ECLAW Queue System — Architecture & Flow

## 1. System Architecture

```mermaid
graph TB
    subgraph Browser["Browser (per user)"]
        UI[UI Panels<br/>join / waiting / ready / controls / result]
        AppJS[app.js<br/>State switching & rendering]
        CtrlJS[controls.js<br/>ControlSocket class]
        StatusWS[Status WebSocket<br/>broadcast viewer]
    end

    subgraph Server["FastAPI Server"]
        API[REST API<br/>/api/queue/join<br/>/api/queue/leave<br/>/api/session/me]
        WSCtrl["/ws/control<br/>ControlHandler<br/>(per-player, authenticated)"]
        WSStatus["/ws/status<br/>StatusHub<br/>(broadcast to all viewers)"]
        SM[StateMachine<br/>TurnState FSM<br/>timers & transitions]
        QM[QueueManager<br/>CRUD on queue_entries]
        GPIO[GPIOController<br/>relay pins]
    end

    subgraph Storage["Storage"]
        DB[(SQLite<br/>queue_entries<br/>game_events)]
    end

    subgraph Hardware["Hardware"]
        Claw[Claw Machine<br/>relays & win sensor]
    end

    AppJS <-->|fetch| API
    CtrlJS <-->|WebSocket| WSCtrl
    StatusWS <-->|WebSocket| WSStatus

    API --> QM
    API --> SM
    WSCtrl --> SM
    WSCtrl --> GPIO
    SM --> QM
    SM --> GPIO
    SM --> WSStatus
    SM --> WSCtrl
    QM --> DB
    GPIO --> Claw
```

## 2. Queue Entry Lifecycle (DB states)

```mermaid
stateDiagram-v2
    [*] --> waiting : POST /api/queue/join
    waiting --> ready : StateMachine.advance_queue()<br/>peek next waiting
    waiting --> cancelled : DELETE /api/queue/leave

    ready --> active : Player sends ready_confirm<br/>via control WebSocket
    ready --> cancelled : DELETE /api/queue/leave<br/>(triggers force_end_turn)
    ready --> done : Ready timeout (15s)<br/>result = skipped

    active --> done : Turn completes<br/>result = win / loss
    active --> done : Hard timeout (90s)<br/>result = expired
    active --> done : Disconnect grace expires<br/>result = expired
    active --> done : Admin skip<br/>result = admin_skipped

    done --> [*]
    cancelled --> [*]
```

## 3. State Machine Turn Flow (TurnState)

```mermaid
stateDiagram-v2
    direction TB

    IDLE --> READY_PROMPT : advance_queue()<br/>next waiting player found

    READY_PROMPT --> MOVING : Player sends ready_confirm<br/>(starts hard turn timer 90s)
    READY_PROMPT --> IDLE : Timeout 15s → skip<br/>or player leaves → force_end

    state "Try Loop (up to 2 tries)" as TryLoop {
        MOVING --> DROPPING : Player clicks drop<br/>or move timeout (30s) auto-drops
        DROPPING --> POST_DROP : Drop hold timeout<br/>(max 10s, relay off)
        POST_DROP --> MOVING : No win + tries left<br/>→ start next try
    }

    POST_DROP --> TURN_END : Win detected!<br/>result = win
    POST_DROP --> TURN_END : No win + no tries left<br/>result = loss
    MOVING --> TURN_END : Hard turn timeout (90s)
    DROPPING --> TURN_END : Hard turn timeout (90s)

    TURN_END --> IDLE : Cleanup complete<br/>→ advance_queue()

    note right of IDLE
        Periodic safety net (10s)
        checks for stuck states
    end note
```

## 4. Player Join & Turn Flow (Sequence)

```mermaid
sequenceDiagram
    participant B as Browser
    participant API as REST API
    participant DB as SQLite
    participant SM as StateMachine
    participant WS as Control WS
    participant Hub as Status Hub
    participant HW as GPIO/Claw

    Note over B: User fills name + email
    B->>API: POST /api/queue/join
    API->>DB: INSERT queue_entries (state=waiting)
    API->>Hub: broadcast queue_update
    API-->>B: {token, position}
    B->>B: localStorage.setItem(token)
    B->>B: switchToState("waiting")

    B->>WS: Connect + auth {token}
    WS-->>B: auth_ok {state, position}

    Note over SM: advance_queue() picks next
    SM->>DB: SET state = ready
    SM->>WS: ready_prompt {timeout: 15s}
    SM->>Hub: broadcast state_update
    WS-->>B: ready_prompt
    B->>B: switchToState("ready")<br/>show countdown

    Note over SM: _ready_timeout starts (15s)

    B->>WS: ready_confirm
    WS->>SM: handle_ready_confirm()
    SM->>DB: SET state = active
    SM->>HW: pulse coin
    SM->>SM: Enter MOVING (try 1)
    SM->>Hub: broadcast state_update
    SM->>WS: state_update {state: moving}
    WS-->>B: state_update
    B->>B: switchToState("active")<br/>start timer

    loop Player controls claw (up to 30s)
        B->>WS: keydown {key: north}
        WS->>HW: direction_on(north)
        WS-->>B: control_ack
        B->>WS: keyup {key: north}
        WS->>HW: direction_off(north)
    end

    B->>WS: drop_start
    WS->>SM: handle_drop_press()
    SM->>HW: all_directions_off + drop_on
    SM->>SM: Enter DROPPING
    SM->>Hub: broadcast state_update

    Note over SM: Drop hold timeout → drop_off
    SM->>HW: drop_off
    SM->>SM: Enter POST_DROP
    SM->>HW: register win callback

    alt Win sensor triggered
        HW->>SM: win callback
        SM->>SM: Enter TURN_END (result=win)
    else No win, tries left
        SM->>SM: Start try 2 → MOVING
        Note over SM: Repeat try loop
    else No win, no tries left
        SM->>SM: Enter TURN_END (result=loss)
    end

    SM->>DB: SET state=done, result, tries_used
    SM->>Hub: broadcast turn_end
    SM->>WS: turn_end {result}
    WS-->>B: turn_end
    B->>B: switchToState("done")
    SM->>SM: Reset to IDLE → advance_queue()
```

## 5. Page Refresh Recovery Flow

```mermaid
sequenceDiagram
    participant B as Browser (refreshed)
    participant API as REST API
    participant WS as Control WS
    participant SM as StateMachine

    Note over B: Page loads, token in localStorage

    B->>API: GET /api/session/me {Bearer token}

    alt State = waiting / ready / active
        API-->>B: {state, position, tries_left}
        B->>B: switchToState(state)
        B->>WS: Connect + auth {token}

        alt Reconnecting active player
            Note over WS: Cancel grace period timer
            WS-->>B: auth_ok {state: active}
            WS-->>B: state_update {try, max_tries, move_seconds}
            B->>B: Resume controls with correct timer
        else Reconnecting waiting player
            WS-->>B: auth_ok {state: waiting}
            B->>B: Show waiting panel
        else Reconnecting ready player
            WS-->>B: auth_ok {state: ready}
            B->>B: Show ready panel + countdown
        end

    else State = done / cancelled
        API-->>B: {state: done}
        B->>B: Clear localStorage token
        B->>B: switchToState(null) → join screen
    else 401 / invalid token
        API-->>B: 401
        B->>B: Clear localStorage token
    end
```

## 6. Disconnect & Recovery Flow

```mermaid
sequenceDiagram
    participant B as Browser
    participant WS as Control WS
    participant SM as StateMachine
    participant HW as GPIO

    Note over B: User navigates away / closes tab

    B--xWS: WebSocket disconnects

    WS->>SM: handle_disconnect(entry_id)
    SM->>HW: all_directions_off()
    WS->>WS: Start grace period (300s)

    alt User returns within grace period
        B->>WS: New connection + auth
        WS->>WS: Cancel grace period
        WS-->>B: auth_ok + state_update
        Note over B: Resumes play
    else Grace period expires
        WS->>SM: handle_disconnect_timeout()
        SM->>SM: _end_turn("expired")
        SM->>SM: Reset to IDLE → advance_queue()
    end
```

## 7. Safety Nets & Recovery

```mermaid
flowchart TB
    subgraph Timers["Per-Turn Timers"]
        RT["Ready Timeout<br/>15s → skip player"]
        MT["Move Timeout<br/>30s → auto-drop"]
        DT["Drop Hold Timeout<br/>10s → release relay"]
        PDT["Post-Drop Timeout<br/>8s → next try or loss"]
        HT["Hard Turn Timeout<br/>90s → expire turn"]
        GT["Grace Period<br/>300s → expire on disconnect"]
    end

    subgraph Periodic["Periodic Safety Net (every 10s)"]
        PC1{"SM == IDLE<br/>& waiting > 0?"}
        PC2{"SM != IDLE<br/>& active entry<br/>done/cancelled<br/>in DB?"}
        ADV[advance_queue]
        REC[_force_recover<br/>→ IDLE → advance]
    end

    subgraph Crash["Timer Crash Recovery"]
        EX[Exception in any timer]
        FR[_force_recover<br/>cancel timers<br/>emergency_stop GPIO<br/>complete entry as error<br/>reset to IDLE<br/>advance_queue]
    end

    PC1 -->|Yes| ADV
    PC2 -->|Yes| REC
    EX --> FR

    subgraph Startup["Server Restart"]
        CS["cleanup_stale()<br/>active entries past grace → expired<br/>ready entries → expired"]
        AQ["advance_queue()<br/>resume if waiting players exist"]
    end

    CS --> AQ
```

## 8. WebSocket Message Types

```mermaid
graph LR
    subgraph ControlWS["Control WebSocket (per player)"]
        direction TB
        subgraph ClientToServer["Client → Server"]
            A1["auth {token}"]
            A2["ready_confirm"]
            A3["keydown {key}"]
            A4["keyup {key}"]
            A5["drop_start"]
            A6["latency_ping"]
        end
        subgraph ServerToClient["Server → Client"]
            B1["auth_ok {state, position}"]
            B2["ready_prompt {timeout_seconds}"]
            B3["state_update {state, try, max_tries, move_seconds}"]
            B4["turn_end {result, tries_used}"]
            B5["control_ack {key, active}"]
            B6["latency_pong"]
            B7["error {message}"]
        end
    end

    subgraph StatusWS["Status WebSocket (all viewers)"]
        direction TB
        subgraph Broadcast["Server → All Clients"]
            C1["queue_update {queue_length, current_player,<br/>viewer_count, entries[]}"]
            C2["state_update {state, active_entry_id,<br/>current_try, max_tries}"]
            C3["turn_end {entry_id, result}"]
        end
    end
```
