import os
import random
import shutil
import threading
import time
import uuid
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from mutagen import File as MutagenFile
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


DEFAULT_DURATION = 240.0


def parse_allowed_origins():
    raw = os.environ.get("ALLOWED_ORIGINS", "*").strip()
    if not raw or raw == "*":
        return "*"
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def mp3_only(filename):
    return Path(filename).suffix.lower() == ".mp3"


def validate_audio_file(path):
    try:
        audio = MutagenFile(path)
    except Exception as exc:
        raise ValueError("Il file caricato non sembra un audio MP3 valido.") from exc

    if audio is None or getattr(audio, "info", None) is None:
        raise ValueError("Il file caricato non sembra un audio MP3 valido.")

    duration = getattr(audio.info, "length", 0) or 0
    if duration <= 0:
        raise ValueError("Il file audio non ha una durata valida.")

    return float(duration)


def title_from_audio(path):
    audio = MutagenFile(path, easy=True)
    duration = DEFAULT_DURATION

    if audio is not None:
        if getattr(audio, "info", None) is not None and getattr(audio.info, "length", None):
            duration = float(audio.info.length)

    title = Path(path).stem.replace("-", " ")

    return title, duration


def build_app():
    app = Flask(__name__)
    allowed_origins = parse_allowed_origins()
    radio_dir = os.environ.get("RADIO_DIR", "./radio")
    staging_dir = os.environ.get("STAGING_DIR", "./staging")
    max_upload_mb = int(os.environ.get("MAX_UPLOAD_MB", "50"))
    admin_password_hash = generate_password_hash(required_env("ADMIN_PASSWORD"))

    app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
    app.config["MAX_CONTENT_LENGTH"] = max_upload_mb * 1024 * 1024

    Path(radio_dir).mkdir(parents=True, exist_ok=True)
    Path(staging_dir).mkdir(parents=True, exist_ok=True)

    CORS(app, origins=allowed_origins)
    socketio = SocketIO(app, cors_allowed_origins=allowed_origins, async_mode="threading")

    state_lock = threading.Lock()
    state = {
        "playlist": [],
        "current_index": 0,
        "started_at": time.time(),
        "track_seq": 0,
    }
    listening_sids = set()
    skip_votes = set()

    def is_origin_allowed(origin):
        if not origin:
            return False
        if allowed_origins == "*":
            return True
        return origin in allowed_origins

    def listeners_payload():
        with state_lock:
            return {"listening": len(listening_sids)}

    def broadcast_listeners():
        socketio.emit("listeners", listeners_payload())

    def needed_votes_unlocked():
        return (len(listening_sids) // 2) + 1 if listening_sids else 0

    def needed_votes():
        with state_lock:
            return needed_votes_unlocked()

    def skip_payload():
        with state_lock:
            return {
                "votes": len(skip_votes),
                "needed": needed_votes_unlocked(),
                "listening": len(listening_sids),
            }

    def broadcast_skip():
        socketio.emit("skip_state", skip_payload())

    def advance_track_locked(replacement_playlist=None):
        if replacement_playlist is not None:
            state["playlist"] = replacement_playlist
            state["current_index"] = 0
        else:
            playlist = state["playlist"]
            if not playlist:
                return False

            if state["current_index"] + 1 >= len(playlist):
                state["playlist"] = load_playlist()
                state["current_index"] = 0
            else:
                state["current_index"] += 1

        state["started_at"] = time.time()
        state["track_seq"] += 1
        skip_votes.clear()
        return True

    def emit_track_advanced():
        socketio.emit("radio_update", current_payload())
        broadcast_skip()

    def advance_track(reason, replacement_playlist=None):
        with state_lock:
            advanced = advance_track_locked(replacement_playlist)

        if advanced:
            emit_track_advanced()

        return advanced

    @app.after_request
    def add_radio_cors_headers(response):
        if request.path.startswith("/radio/"):
            origin = request.headers.get("Origin")
            if is_origin_allowed(origin):
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Range, Content-Type"
                response.headers["Access-Control-Expose-Headers"] = "Accept-Ranges, Content-Length, Content-Range"
        return response

    def load_playlist():
        directory = Path(radio_dir)
        directory.mkdir(parents=True, exist_ok=True)
        tracks = []

        for path in sorted(directory.glob("*.mp3")):
            try:
                title, duration = title_from_audio(path)
            except Exception:
                title = path.stem.replace("_", " ")
                duration = DEFAULT_DURATION

            tracks.append(
                {
                    "filename": path.name,
                    "url": f"/radio/{path.name}",
                    "title": title,
                    "duration": duration,
                }
            )

        random.shuffle(tracks)
        return tracks

    def refresh_playlist_preserving_current():
        new_playlist = load_playlist()
        with state_lock:
            current_track = None
            if state["playlist"]:
                current_track = state["playlist"][state["current_index"]]

            if current_track:
                current_filename = current_track["filename"]
                remaining = [track for track in new_playlist if track["filename"] != current_filename]
                if len(remaining) != len(new_playlist):
                    state["playlist"] = [current_track] + remaining
                    state["current_index"] = 0
                else:
                    state["playlist"] = new_playlist
                    state["current_index"] = 0
                    state["started_at"] = time.time()
            else:
                state["playlist"] = new_playlist
                state["current_index"] = 0
                state["started_at"] = time.time()

            return len(state["playlist"])

    def delete_track_from_playlist(filename):
        deleted_current = False
        new_playlist = load_playlist()

        with state_lock:
            current_track = None
            if state["playlist"]:
                current_track = state["playlist"][state["current_index"]]

            if current_track and current_track["filename"] != filename:
                remaining = [track for track in new_playlist if track["filename"] != current_track["filename"]]
                state["playlist"] = [current_track] + remaining
                state["current_index"] = 0
            else:
                deleted_current = current_track is not None and current_track["filename"] == filename
                if not deleted_current:
                    state["playlist"] = new_playlist
                    state["current_index"] = 0
                    state["started_at"] = time.time()

            return len(new_playlist if deleted_current else state["playlist"]), deleted_current, new_playlist

    def current_payload():
        with state_lock:
            playlist = list(state["playlist"])
            current_index = state["current_index"]
            started_at = state["started_at"]
            track_seq = state["track_seq"]

            if playlist:
                track = playlist[current_index]
                duration = track["duration"]
            else:
                track = None
                duration = 0.0

            return {
                "server_time": time.time(),
                "current_index": current_index,
                "playlist_length": len(playlist),
                "started_at": started_at,
                "track_seq": track_seq,
                "duration": duration,
                "track": track,
            }

    def reset_playlist():
        playlist = load_playlist()
        with state_lock:
            state["playlist"] = playlist
            state["current_index"] = 0
            state["started_at"] = time.time()
        return playlist

    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("admin_authenticated"):
                if request.method == "POST":
                    return jsonify({"ok": False, "error": "Autenticazione richiesta."}), 401
                return redirect(url_for("admin_login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def list_radio_tracks():
        directory = Path(radio_dir)
        tracks = []
        for path in sorted(directory.glob("*.mp3")):
            try:
                title, duration = title_from_audio(path)
            except Exception:
                title = path.stem.replace("_", " ")
                duration = DEFAULT_DURATION
            tracks.append({"filename": path.name, "title": title, "duration": duration})
        return tracks

    def unique_output_path(stem):
        radio_path = Path(radio_dir)
        candidate = radio_path / f"{stem}.mp3"
        counter = 2
        while candidate.exists():
            candidate = radio_path / f"{stem}-{counter}.mp3"
            counter += 1
        return candidate

    def radio_file_path(filename):
        filename = filename or ""
        if not filename or Path(filename).name != filename or not mp3_only(filename):
            raise ValueError("Nome file non valido.")

        radio_path = Path(radio_dir).resolve()
        target = (radio_path / filename).resolve()
        if target.parent != radio_path:
            raise ValueError("Nome file non valido.")
        return target

    def radio_worker():
        while True:
            with state_lock:
                playlist = state["playlist"]
                if not playlist:
                    needs_reload = True
                    sleep_for = 5.0
                else:
                    needs_reload = False
                    track = playlist[state["current_index"]]
                    elapsed = time.time() - state["started_at"]
                    sleep_for = max(0.2, track["duration"] - elapsed)

            if needs_reload:
                socketio.sleep(sleep_for)
                reset_playlist()
                continue

            socketio.sleep(sleep_for)

            advanced = False
            with state_lock:
                playlist = state["playlist"]
                if not playlist:
                    continue

                track = playlist[state["current_index"]]
                if time.time() - state["started_at"] + 0.05 < track["duration"]:
                    continue

                advanced = advance_track_locked()

            if advanced:
                emit_track_advanced()

    @app.route("/")
    @app.route("/player")
    def player():
        return render_template("player.html")

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        error = None
        if request.method == "POST":
            password = request.form.get("password", "")
            if check_password_hash(admin_password_hash, password):
                session["admin_authenticated"] = True
                return redirect(request.args.get("next") or url_for("admin"))
            error = "Password non valida."
        return render_template("admin_login.html", error=error)

    @app.route("/admin/logout", methods=["POST"])
    @admin_required
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login"))

    @app.route("/admin")
    @admin_required
    def admin():
        return render_template(
            "admin.html",
            tracks=list_radio_tracks(),
            max_upload_mb=max_upload_mb,
        )

    @app.route("/admin/tracks", methods=["POST"])
    @admin_required
    def admin_tracks():
        upload = request.files.get("track")

        if not upload or not upload.filename:
            return jsonify({"ok": False, "error": "Carica un file MP3."}), 400

        content_type = (upload.mimetype or "").lower()
        if content_type and content_type not in {"audio/mpeg", "audio/mp3", "audio/x-mpeg", "application/octet-stream"}:
            return jsonify({"ok": False, "error": "Il file caricato non risulta un audio MP3."}), 400

        original_name = secure_filename(upload.filename)
        if not original_name or not mp3_only(original_name):
            return jsonify({"ok": False, "error": "Sono ammessi solo file .mp3."}), 400

        stem = secure_filename(Path(original_name).stem).strip("._")
        if not stem:
            return jsonify({"ok": False, "error": "Nome file non valido."}), 400

        upload_path = Path(staging_dir) / f"{stem}-{uuid.uuid4().hex}.mp3"
        final_path = unique_output_path(stem)
        temp_output = final_path.with_name(f".{final_path.stem}.{uuid.uuid4().hex}.tmp.mp3")

        try:
            upload.save(upload_path)
            validate_audio_file(upload_path)
            shutil.copyfile(upload_path, temp_output)
            temp_output.replace(final_path)
            total_tracks = refresh_playlist_preserving_current()
            return jsonify(
                {
                    "ok": True,
                    "message": f"Brano pubblicato: {final_path.name}",
                    "filename": final_path.name,
                    "tracks": list_radio_tracks(),
                    "playlist_length": total_tracks,
                }
            )
        except Exception as exc:
            for path in (temp_output, final_path):
                if path.exists():
                    path.unlink()
            if isinstance(exc, ValueError) and upload_path.exists():
                upload_path.unlink()
                return jsonify({"ok": False, "error": str(exc)}), 400
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/admin/tracks/delete", methods=["POST"])
    @admin_required
    def admin_delete_track():
        filename = request.form.get("filename", "")

        try:
            target = radio_file_path(filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if not target.exists():
            return jsonify({"ok": False, "error": "Brano non trovato."}), 404

        try:
            target.unlink()
            total_tracks, deleted_current, replacement_playlist = delete_track_from_playlist(target.name)
            payload = {
                "ok": True,
                "message": f"Brano eliminato: {target.name}",
                "filename": target.name,
                "tracks": list_radio_tracks(),
                "playlist_length": total_tracks,
            }
            if deleted_current:
                advance_track("delete", replacement_playlist)
            return jsonify(payload)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/radio/<path:filename>")
    def radio_file(filename):
        return send_from_directory(radio_dir, filename, conditional=True)

    @app.route("/api/radio/state")
    def api_radio_state():
        return jsonify(current_payload())

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(error):
        return jsonify({"ok": False, "error": f"File troppo grande. Limite: {max_upload_mb} MB."}), 413

    @socketio.on("connect")
    def on_connect():
        emit("radio_state", current_payload())
        emit("listeners", listeners_payload())
        emit("skip_state", skip_payload())

    @socketio.on("request_state")
    def on_request_state():
        emit("radio_state", current_payload())

    @socketio.on("set_listening")
    def on_set_listening(is_listening):
        should_advance = False
        with state_lock:
            if bool(is_listening):
                listening_sids.add(request.sid)
            else:
                listening_sids.discard(request.sid)
                skip_votes.discard(request.sid)
            should_advance = bool(listening_sids) and len(skip_votes) >= needed_votes_unlocked()

        if should_advance:
            advance_track("vote")
        else:
            broadcast_skip()

        broadcast_listeners()

    @socketio.on("vote_skip")
    def on_vote_skip():
        should_advance = False
        listeners_changed = False
        with state_lock:
            if request.sid not in listening_sids:
                listening_sids.add(request.sid)
                listeners_changed = True

            if request.sid in skip_votes:
                skip_votes.discard(request.sid)
            else:
                skip_votes.add(request.sid)

            should_advance = bool(listening_sids) and len(skip_votes) >= needed_votes_unlocked()

        if should_advance:
            advance_track("vote")
        else:
            broadcast_skip()

        if listeners_changed:
            broadcast_listeners()

    @socketio.on("disconnect")
    def on_disconnect():
        should_advance = False
        with state_lock:
            listening_sids.discard(request.sid)
            skip_votes.discard(request.sid)
            should_advance = bool(listening_sids) and len(skip_votes) >= needed_votes_unlocked()

        if should_advance:
            advance_track("vote")
        else:
            broadcast_skip()

        broadcast_listeners()

    reset_playlist()
    socketio.start_background_task(radio_worker)
    return app, socketio


app, socketio = build_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    os.environ["PORT"] = str(port)
    socketio.run(app, host="0.0.0.0", port=int(os.environ["PORT"]), allow_unsafe_werkzeug=True)
