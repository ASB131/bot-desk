# Bot desk

A Discord bot with a small web panel. It stays online in Docker, posts scheduled messages and pictures, keeps a countdown in a channel, and can change its profile photo on a weekly schedule.

The panel is password-protected on your network. Do not expose its port to the public internet.

## Create the Discord bot

1. Open https://discord.com/developers/applications and create an application.
2. Open **Bot**, click **Reset Token**, and copy the token.
3. Under **Bot**, turn on **Message Content Intent** so emoji reactions work. Leave the other privileged intents off.
4. Copy **Application ID** and **Public Key** from **General Information** into `.env` as `DISCORD_APPLICATION_ID` and `DISCORD_PUBLIC_KEY`.
5. Put the bot token in `.env` as `DISCORD_TOKEN`. Start the container, open the panel, and use the invite link on the overview page.

Or invite it yourself. Scopes: `bot` and `applications.commands`. Permissions: View Channels, Send Messages, Embed Links, Attach Files, Read Message History.

## Run with Docker (recommended)

Requires [Docker](https://docs.docker.com/get-docker/) with Compose.

```bash
git clone https://github.com/ASB131/bot-desk.git
cd bot-desk
cp .env.example .env
# Edit .env: DISCORD_TOKEN and PANEL_PASSWORD (required)
docker compose pull
docker compose up -d
```

Open http://localhost:23435 and log in with `PANEL_PASSWORD`. Uploads and schedules live in the `./data` folder.

The image is built on GitHub Actions and published to **GitHub Container Registry** as `ghcr.io/asb131/bot-desk:latest`. After you change only `.env` or `data`, `docker compose pull && docker compose up -d` is enough.

### Build the image locally

If you changed the source code or the registry image is not available yet:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

## Move to another machine

Copy the project folder (or clone from GitHub), including `.env` and `data` if you already configured events or uploaded pictures.

```bash
docker compose pull
docker compose up -d
```

Open `http://SERVER_IP:23435` from your LAN. Allow the chosen port through the host firewall for local access only.

## What you can do

- **Events** — Channel, time, message, optional picture; `{name}`, `{time}`, and `{countdown}` placeholders.
- **Countdown** — One channel message updates about once a minute; bot status shows the next event.
- **Library** — Shared picture pool (PNG, JPEG, GIF, WebP). Used by slash commands, reactions, facts, and schedules.
- **Commands** — Configurable `/picture` (random library image, no repeat until the deck cycles) and `/countdown`. Rename the picture command on the Commands page.
- **Facts** — Random text on an interval; optional library image per post.
- **Pictures** — Scheduled library images only, same no-repeat deck as `/picture`.
- **Reactions** — Emoji → random library reply.
- **Profile photo** — From the library, from an event, or on a weekly slot.

If the bot is offline when an event starts, it still posts after reconnect within 12 hours. Older missed events are marked missed.

Slash commands appear after the bot is online in a server. If they are missing, restart the container: `docker compose restart`.
