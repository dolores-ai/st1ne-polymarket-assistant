# VPS Setup Plan

## Goal
Establish a reliable, secure, and always-on Virtual Private Server (VPS) to host the Polymarket bot and future projects.

## 1. Provider Selection
**Recommendation: Hetzner (CX22) or DigitalOcean (Basic Droplet)**
-   **Cost**: ~$4-6/month.
-   **Specs**: 2GB RAM (Minimum for safety with multiple bots), 1 vCPU.
-   **Location**: Ashburn (US East) or Germany (Hetzner) - closer to major exchanges is better but for this strategy (10s refresh), location is less critical than reliability.

## 2. Server Configuration
-   **OS**: Ubuntu 24.04 LTS (Stable, standard).
-   **Security**:
    -   SSH Key authentication only (Disable password login).
    -   UFW Firewall (Allow only SSH 22).
    -   Fail2Ban (Prevent brute force).

## 3. Environment Setup
-   **Python**: Install Python 3.12+ (via `deadsnakes` ppa or standard repos).
-   **Package Manager**: `uv` or `pip`.
-   **Process Manager**: `systemd` (Built-in, robust) or `PM2` (easier logs).
    -   *Decision*: **systemd** is recommended for "set and forget" reliability.

## 4. Deployment Workflow
1.  **Git**: Clone your repo from GitHub (you will need to push your local changes first).
2.  **Env**: Securely copy `.env` (SCP or paste).
3.  **Install**: `pip install -r requirements.txt`.
4.  **Service**: Create `/etc/systemd/system/polymate.service`.
5.  **Enable**: `systemctl enable polymate && systemctl start polymate`.

## 5. Maintenance
-   **Logs**: `journalctl -u polymate -f` to watch live.
-   **Updates**: `git pull && systemctl restart polymate`.
