# Francho Poster (Telethon + Aiogram)

Publicador desde cuenta personal con panel de control por bot:
- Login por código/2FA (Telethon)
- Listas de destinos
- Origen (canal/grupo/usuario)
- Envío “copiando” (texto/medios, conserva emojis premium)
- Programación por intervalos (APScheduler)

## Requisitos
- Python 3.10+
- Credenciales de Telegram en `.env` (ver `.env.example`)

## Instalación local
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env   # complete variables
python main.py
