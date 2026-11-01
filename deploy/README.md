# Deploying the alpha on AWS

One small server runs the app and its HTTPS front door in Docker; the database is a managed PostgreSQL (RDS). About **$50 a month** plus Claude usage.

> **Read this first.** The Dockerfile and compose file were written on a machine without Docker, so they have **not been built yet**. Expect the first
> `docker compose up --build` to be the real test. If something fails, the error will say which line; it is usually a small fix.

## What you need

| | |
|---|---|
| AWS account | with MFA on the root user, a monthly **budget alert**, and a normal admin user for daily use |
| Domain | one you own, with DNS you can edit (Route 53, or any registrar) |
| Google Cloud | the OAuth client you already have, with the new address added |
| Anthropic | an API key, with a **monthly spend limit** set in the Anthropic console |

## 1. Network (once)

In the AWS console, pick a region near you (the rest of this assumes the default VPC) and make two security groups (EC2, then Security Groups):

- `web`: inbound **80** and **443** from anywhere, **22** from *My IP* only.
- `db`: inbound **5432** from the `web` group only (choose the group, not an address).

## 2. Database (RDS)

RDS, Create database: *Standard create*, **PostgreSQL 17**, template *Dev/Test*, single instance, identifier `northhelm-db`, master user `postgres` with a generated password (save it), class
**db.t4g.micro**, storage gp3 20 GiB, **Public access: No**, security group `db`, **automated backups: 7 days**. Create it and note the endpoint name.

## 3. Server (EC2)

Launch an instance: **Ubuntu Server 24.04 LTS**, **t3.medium**, 30 GiB gp3, a new key pair (keep the `.pem`), security group `web`. Then Elastic IPs, allocate one and associate it with the instance (so the address never changes).

Point your domain at that IP: an **A record** for the domain (and `www` if you like). In Route 53 that is a hosted zone, Create record.

Connect and install Docker:

```bash
ssh -i your-key.pem ubuntu@YOUR_ELASTIC_IP
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu && exit        # log in again so the group applies
```

## 4. Code and settings

```bash
git clone https://github.com/Halskii/job-finder.git && cd job-finder
cp deploy/env.production.example .env && chmod 600 .env
nano .env          # fill in DOMAIN, DATABASE_URL, ANTHROPIC_API_KEY, GOOGLE_CLIENT_ID
```

`deploy/config.production.yaml` holds the service settings (models, daily limits, the daily fetch and monthly crawl). Look it over; the defaults are cautious.

Create the app's database and user (run from the server; `ENDPOINT` and the passwords are yours):

```bash
docker run --rm -it postgres:17 psql "postgresql://postgres:MASTERPASSWORD@ENDPOINT:5432/postgres" \
  -c "CREATE ROLE jobfinder LOGIN PASSWORD 'CHOOSE-A-PASSWORD'" \
  -c "GRANT jobfinder TO postgres" \
  -c "CREATE DATABASE jobfinder OWNER jobfinder"
```

Use that password in `DATABASE_URL`. The app creates its own tables the first time it connects.

## 5. Google sign-in

Google Cloud console, APIs & Services, Credentials, your OAuth client: under **Authorized JavaScript origins** add `https://YOURDOMAIN`. Under *OAuth consent screen*, set the publishing status to **In production** (otherwise only listed test users can sign in).

## 6. Copy your data up (recommended)

A fresh server knows no company boards and no postings, so its first start would spend hours crawling and fetching. Copying yours skips that, and keeps your own account. On your PC (PowerShell):

```powershell
& "C:\Program Files\PostgreSQL\17\bin\pg_dump.exe" -Fc -n public --no-owner --no-acl -f jobfinder.dump "YOUR_LOCAL_DATABASE_URL"
scp -i your-key.pem jobfinder.dump ubuntu@YOUR_ELASTIC_IP:~/
scp -i your-key.pem -r data\users ubuntu@YOUR_ELASTIC_IP:~/users
```

On the server, from the `job-finder` folder (an "already exists" error for schema `public` is harmless):

```bash
docker compose build app
docker run --rm -v ~/jobfinder.dump:/d/jobfinder.dump postgres:17 pg_restore --no-owner --no-acl -d "$(grep ^DATABASE_URL .env | cut -d= -f2-)" /d/jobfinder.dump
docker compose run --rm --no-deps -v "$HOME/users:/src:ro" app sh -c 'mkdir -p /app/data/users && cp -r /src/. /app/data/users/'
```

## 7. Start it

```bash
docker compose up -d --build
docker compose logs -f app          # Ctrl+C to stop watching
```

Then let people in. Sign-ups are **invite-only**: an email can only create an account if it was invited first. Your own account came with the data you copied.

```bash
docker compose run --rm app python main.py invite add friend@example.com other@example.com --note "alpha"
docker compose run --rm app python main.py invite list
```

## 8. Check it works

Open `https://YOURDOMAIN`, then:

1. The padlock shows and `/healthz` answers `{"ok":true}`.
2. Sign in with Google (your account, with its jobs).
3. A second browser: an uninvited email is refused with the invite message; an invited one can sign up, upload a resume, read it, save titles, and watch matches appear.
4. Tailor a resume and download the PDF (this checks the browser inside the container).
5. `docker compose logs app` shows no errors.

## Running it

| | |
|---|---|
| Logs | `docker compose logs -f app` |
| Update | `git pull && docker compose up -d --build` (tables upgrade themselves) |
| **Pause all paid features** | set `paused: true` under `limits:` in `deploy/config.production.yaml`; it takes effect at once |
| Change limits | the same file; `limits:` applies at once, other settings after `docker compose restart app` |
| Daily fetch | automatic (`fetch_every_hours: 24`); run one now with `docker compose run --rm app python main.py fetch` |
| Usage and cost | `docker compose run --rm app python main.py usage` shows each day's paid calls and tokens, for everyone and for each person |
| Backups | RDS keeps 7 days. User files live in a Docker volume on the server's disk, so turn on a **nightly snapshot** (EC2, Elastic Block Store, Lifecycle Manager, default policy). |
| Costs | Anthropic's console shows spending; the limits above cap what the site can use in a day. |

## What the alpha does not have yet

- **One server only.** The background worker is in the app, and files are on its disk. More than one copy would not work yet.
- **No email confirmation or password reset** (they need an email service). Google sign-in covers most people.
- **The privacy page is plain-language, not lawyer-reviewed.** Fine for friends; get it reviewed before opening to strangers.
- **Files are not in S3**, only on the server's disk (snapshots protect them).
