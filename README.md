# Radio

Microservizio Flask + Flask-SocketIO per trasmettere una radio MP3 sincronizzata a piu client.

## Configurazione

Variabili d'ambiente:

- `PORT`: porta HTTP, default `5001`.
- `ALLOWED_ORIGINS`: CSV degli origin ammessi, per esempio `https://aviator.mentealveare.com,https://radio.mentealveare.com`. Se omessa usa `*`.
- `RADIO_DIR`: cartella MP3, default `./radio`.
- `ADMIN_PASSWORD`: password obbligatoria per `/admin`.
- `STAGING_DIR`: cartella per upload grezzi, default `./staging`. Non deve coincidere con `RADIO_DIR`.
- `MAX_UPLOAD_MB`: dimensione massima upload, default `50`.

## Avvio Locale

```bash
pip install -r requirements.txt
python app.py
```

Player pubblico standalone: `http://localhost:5001/`.

Alias player: `http://localhost:5001/player`.

Admin protetto: `http://localhost:5001/admin`.

## Docker

```bash
docker build -t radio .
docker run --rm -p 5001:5001 -v /path/mp3:/app/radio radio
```

## Aggiungere MP3

Inserisci file `.mp3` dentro `RADIO_DIR`.

All'avvio il servizio legge tutti gli MP3, calcola la durata con `mutagen` e crea una playlist mescolata. A fine playlist rilegge `RADIO_DIR`, include eventuali nuovi MP3, rimescola e riparte.

I file `radio/*.mp3` sono ignorati da git. `radio/.gitkeep` mantiene la cartella nel repository.

## Admin Upload

La pagina `/admin` permette di pubblicare nuovi brani tramite login con `ADMIN_PASSWORD`. Tutti gli endpoint POST admin richiedono autenticazione; nessun endpoint di scrittura e accessibile senza sessione admin valida.

Flusso di pubblicazione:

- l'upload MP3 grezzo viene salvato in `STAGING_DIR` con nome sanificato;
- il server accetta solo file `.mp3` e verifica con `mutagen` che siano audio leggibili con durata valida;
- il file validato viene pubblicato in `RADIO_DIR` con nome sanificato;
- il grezzo resta in `STAGING_DIR`;
- gli output parziali vengono puliti in caso di errore;
- la playlist in memoria viene riletta senza interrompere il brano in onda.

Dal pannello `/admin` e anche possibile eliminare brani gia pubblicati in `RADIO_DIR`. L'eliminazione richiede la stessa sessione admin; se viene eliminato il brano attualmente in onda, il servizio passa allo stato disponibile successivo e invia `radio_update`.

Sicurezza: `/admin` e protetto da password, ma in produzione e consigliato tenerlo dietro reverse proxy, allowlist IP, VPN o altra protezione aggiuntiva. Non esporlo pubblicamente su internet senza ulteriori controlli.

## Contratto Endpoint

- `GET /radio/<filename>`: serve gli MP3 da `RADIO_DIR` con `send_from_directory(RADIO_DIR, filename, conditional=True)` per supportare Range/seek. Gli header CORS sono aggiunti per gli origin inclusi in `ALLOWED_ORIGINS`.
- `GET /`: player web pubblico della radio.
- `GET /player`: alias del player web pubblico.
- `GET /admin`: pagina admin protetta da autenticazione.
- `POST /admin/login`: login admin con `ADMIN_PASSWORD`.
- `POST /admin/logout`: logout admin.
- `POST /admin/tracks`: upload autenticato, validazione MP3 e pubblicazione del brano.
- `POST /admin/tracks/delete`: eliminazione autenticata di un brano pubblicato.
- `GET /api/radio/state`: ritorna lo stato corrente in JSON.
- `GET /healthz`: ritorna `200 ok`.

## Contratto Socket.IO

Configurazione server:

```python
SocketIO(app, cors_allowed_origins=ALLOWED_ORIGINS, async_mode="threading")
```

Eventi:

- `connect`: il server emette `radio_state` solo al client connesso.
- `request_state`: il server risponde con `radio_state` solo al richiedente.
- `radio_update`: broadcast a tutti i client quando cambia il brano corrente.

Payload di `radio_state`, `radio_update` e `/api/radio/state`:

```json
{
  "server_time": 1710000000.0,
  "current_index": 0,
  "playlist_length": 10,
  "started_at": 1710000000.0,
  "duration": 240.0,
  "track": {
    "filename": "brano.mp3",
    "url": "/radio/brano.mp3",
    "title": "brano",
    "duration": 240.0
  }
}
```

Se non ci sono MP3, `track` e `null`, `playlist_length` e `0`, `duration` e `0.0`.
