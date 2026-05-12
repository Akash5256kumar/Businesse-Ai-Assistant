# Frontend Integration Prompt — Customer Clarification Flow

Paste this prompt into your frontend project to implement the customer clarification flow.

---

## PROMPT

We have a WhatsApp-style chat UI (Business Assistant). The backend API has been updated to support a **customer clarification flow**. You need to handle 3 new fields in the chat API response and add a new API call.

---

### 1. Updated Chat API Response

`POST /api/v1/chat/`

The response now includes 2 new fields:
                                                                                                                                                                                                                                                                                                                                      
```json
{
  "reply": "Kaun se Sharma ji? Neeche se select karo 👇",
  "transactions": [],
  "confidence": "high",
  "clarification_needed": null,

  "customer_candidates": [
    { "id": 1, "name": "Sharma Pipes", "phone": "9876500000", "pending": 5000.0 },
    { "id": 2, "name": "Sharma Hardware", "phone": "9123400000", "pending": 0.0 }
  ],
  "pending_transaction": {
    "type": "sale",
    "customer_name": "Sharma ji",
    "total_amount": 25000,
    ...
  }
}
```

**3 states to handle:**

| State | Condition | What to show |
|-------|-----------|-------------|
| Normal reply | `customer_candidates` is empty AND `pending_transaction` is null | Show bot message as usual |
| Multiple customers found | `customer_candidates.length >= 2` | Show bot message + customer selection buttons |
| New customer | `customer_candidates` is empty AND `pending_transaction` is NOT null | Show bot message + phone number input |

---

### 2. New Confirm API

`POST /api/v1/chat/confirm-customer/`

Call this after user picks a customer or enters phone number.

**Headers:** Same auth token as chat API.

**Request body (existing customer selected):**
```json
{
  "customer_id": 1,
  "pending_transaction": { ...same object from previous response... }
}
```

**Request body (new customer with phone):**
```json
{
  "customer_id": null,
  "customer_name": "Sharma ji",
  "customer_phone": "9876500000",
  "pending_transaction": { ...same object from previous response... }
}
```

**Request body (new customer, phone skipped):**
```json
{
  "customer_id": null,
  "customer_name": "Sharma ji",
  "customer_phone": "skip",
  "pending_transaction": { ...same object from previous response... }
}
```

**Response:** Same as normal chat response — show `reply` as bot message.

---

### 3. UI Flow to Implement

#### Case A — Multiple customers found

```
Bot message: "Kaun se Sharma ji? Neeche se select karo 👇"

[Sharma Pipes  •  98765xxxxx  •  Baaki: ₹5,000]
[Sharma Hardware  •  91234xxxxx  •  Baaki: ₹0]
```

- Render each `customer_candidates` item as a tappable card/button
- Show: name, last 6 digits of phone (masked), pending amount
- On tap: call `POST /confirm-customer/` with `customer_id` + `pending_transaction`
- After response: clear buttons, show bot reply as new message

#### Case B — New customer (0 candidates, pending_transaction present)

```
Bot message: "XYZ system mein nahi hain. Unka phone number kya hai?"

[Phone input field]        [Skip]
[Submit button]
```

- Show an inline phone input below the bot message
- On submit: call `POST /confirm-customer/` with `customer_phone` + `customer_name` (from `pending_transaction.customer_name`) + `pending_transaction`
- "Skip" button sends `customer_phone: "skip"`
- After response: hide input, show bot reply

---

### 4. State to Store in Frontend

When `pending_transaction` is present in response, store it in local state:

```js
// pseudo-code
if (response.pending_transaction) {
  setPendingTransaction(response.pending_transaction)
  setCustomerCandidates(response.customer_candidates)
}
```

Clear `pendingTransaction` and `customerCandidates` after `confirm-customer` call succeeds.

---

### 5. Quick Reply Buttons (bottom bar) — No Change Needed

The existing `Sale entry`, `Payment received`, `Expense add` buttons are fine as-is. They send a pre-filled message to the normal chat endpoint. The clarification flow kicks in automatically from the backend if needed.

---

### 6. Example Full Flow

```
User taps "Sale entry"
  → sends message "Sale entry" to POST /api/v1/chat/
  → bot: "Kiska sale? Amount aur naam likhiye 😊"

User types: "Sharma ji ko 50 pipes 25000"
  → POST /api/v1/chat/
  ← response: customer_candidates = [Sharma Pipes, Sharma Hardware]
              pending_transaction = { type: "sale", amount: 25000, ... }

UI shows: "Kaun se Sharma ji?" + 2 buttons

User taps [Sharma Pipes]
  → POST /api/v1/chat/confirm-customer/
     { customer_id: 1, pending_transaction: {...} }
  ← response: { reply: "✅ Sale recorded\n💰 ₹25,000\n👤 Sharma Pipes" }

UI shows bot reply, buttons disappear
```
