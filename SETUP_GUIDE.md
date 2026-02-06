# detailed-Polymarket-Bot-Setup-Guide

This guide walks you through everything needed to run your High-Conviction Polymarket Bot, starting from zero.

## 1. Accounts & Wallet Setup

You need a crypto wallet that supports the **Polygon (MATIC)** network. We recommend **Phantom** or **MetaMask**.

### Step 1.1: Install a Wallet
1.  **Download Phantom Wallet** (recommended for ease of use) or **MetaMask** as a browser extension.
2.  Create a new wallet and **WRITE DOWN YOUR SEED PHRASE** on paper. Never share this.

### Step 1.2: Connect to Polymarket
1.  Go to [Polymarket.com](https://polymarket.com/).
2.  Click **Sign Up / Log In**.
3.  Choose **"Log in with Wallet"** and select your Phantom/MetaMask wallet.
4.  Sign the message to authenticate.
5.  (Optional) Enter an email if prompted, but the wallet connection is what matters for the bot.

---

## 2. Funding Your Wallet

The bot needs two things on the **Polygon Network**:
1.  **USDC (approx. $20–50)**: To place trades.
2.  **MATIC (approx. $1–2)**: To pay for one-time "approval" transactions. Actual trading is gasless (paid by Polymarket), but allowing the bot to spend your USDC requires a tiny bit of MATIC once.

### How to get funds on Polygon:
-   **Option A: Buy on an Exchange (Coinbase/Binance)**
    1.  Buy USDC and MATIC.
    2.  Withdraw them to your wallet address.
    3.  **IMPORTANT**: Select **Polygon (MATIC) Network** for the withdrawal. Do NOT send to Ethereum/ERC20.

-   **Option B: Bridge**
    1.  If you have funds on Ethereum or Solana, use a bridge like [Portal](https://portalbridge.com/) or [Jumper.exchange](https://jumper.exchange/) to move USDC to Polygon.

---

## 3. Export Your Private Key

The bot needs your private key to sign trades on your behalf.

### Using Phantom:
1.  Open the Phantom extension.
2.  Click **Settings (Gear icon)** -> **Manage Accounts**.
3.  Select your account (e.g., "Account 1").
4.  Click **Show Private Key**.
5.  Copy the string. It usually looks like a long string of numbers/letters.

**SECURITY WARNING:**
-   **NEVER** share this key with anyone.
-   **NEVER** commit this key to GitHub.
-   Only paste it into the `.env` file on your secure computer.

---

## 4. Configure the Bot

1.  Navigate to the project folder (`PolyMate` or `polymarket-assistant`).
2.  Copy the example configuration file:
    ```bash
    cp .env.example .env
    ```
3.  Open `.env` in a text editor (Notepad, VS Code).
4.  Paste your private key:
    ```ini
    PRIVATE_KEY=YOUR_EXPORTED_KEY_HERE
    POLYGON_RPC=https://polygon-rpc.com
    DRY_RUN=True
    ```

---

## 5. First Run & Approvals

Before the bot can trade, you must "approve" the Polymarket contracts to spend your USDC.

1.  Open your terminal in the project folder.
2.  Run the setup script:
    ```bash
    python test_approve_and_smoke.py
    ```
3.  **Watch the output:**
    -   It will connect to the API.
    -   It will check if USDC is approved. If not, it will send an "Approve" transaction (costs ~0.01 MATIC).
    -   It will check "Conditional Tokens". If not approved, it sends another tx.
    -   **Wait** until you see "Success! Client initialized" and "Approved".

---

## 6. Running the Dashboard

### Dry-Run Mode (Safe)
Start by running the bot without real trading to see how it works.

1.  Ensure `.env` has `DRY_RUN=True`.
2.  Run:
    ```bash
    python main.py
    ```
3.  Select a coin (e.g., BTC) and timeframe (e.g., 15m).
4.  Watch the "Execution Client Initialized: DRY RUN" message (yellow).
5.  When a signal triggers (Score 8/10), it will print **"[DRY-RUN] TRIGGER_BUY..."** but NO money will move.

### Live Trading Mode (Real Money)
Once you are confident:

1.  Edit `.env` and change `DRY_RUN=False`.
2.  Restart the bot:
    ```bash
    python main.py
    ```
3.  **Warning**: The bot will now spend up to **$5 USDC** per trade when signals trigger.

---

## 7. Monitoring & Stopping

-   **Logs**: Check `trade_log.txt` in the folder to see trade history.
-   **Stop**: Press `Ctrl+C` in the terminal to stop the bot immediately.
-   **Panic Switch**: If the bot goes crazy, simply close the terminal or delete the `.env` file.

### Risk Management (Default Hardcoded Limits)
-   **Max Position**: $5.00 per trade.
-   **Max Daily Loss**: -$15.00 (Bot stops trading if you lose this much in a day).
-   **Cooldown**: 5 minutes between trades on the same market.
