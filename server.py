"""
GAS Tube 自前ストリームプロキシ (yt-dlp)
- GASからは /streams/<videoId> を叩く -> GAS版と同じ正規化JSONを返す
- 実際の動画データは /playback?u=<encoded googlevideo URL> が Range付きで中継する
  (googlevideoのURLは取得したIPに紐づくことがあるため、ブラウザへ直接渡さず中継するのが安定)
使い方:
  pip install yt-dlp requests curl_cffi
  python server.py            # http://localhost:8080 で起動
  GASの Code.gs の STREAM_PROXY_BASE にこのサーバーの公開URLを設定する
デプロイ: Render / Railway / Cloud Run / 自宅PC(Cloudflare Tunnel) など。
  Dockerfile同梱。環境変数 PORT に対応。
注意: 自分が視聴する用途に限定してください。YouTubeの利用規約に従うこと。

v13 (2026-11-15): RenderのIPではデフォルト取得が "Failed to extract any
player response" で全滅したため、取得戦略を複数試す方式に変更。
Chrome偽装(impersonate) -> TV埋め込み -> Android VR の順に試し、
720p以上が取れた時点で打ち切る。取れた中で一番高画質の結果を返す。
"""
import json, os, re, subprocess, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

CACHE = {}  # videoId -> (timestamp, payload)
import time

STRATEGIES = [
    ["--impersonate", "chrome"],
    ["--impersonate", "chrome", "--extractor-args", "youtube:player_client=tv_embedded"],
    ["--extractor-args", "youtube:player_client=tv_embedded"],
    ["--extractor-args", "youtube:player_client=android_vr"],
    [],
]

def fetch_info(video_id: str) -> dict:
    last_err = ""
    best = None  # (max_height, info)
    for args in STRATEGIES:
        try:
            out = subprocess.run(
                [sys.executable, "-m", "yt_dlp", "-J", "--no-warnings", "--no-playlist",
                 *args, f"https://www.youtube.com/watch?v={video_id}"],
                capture_output=True, text=True, timeout=45)
            if out.returncode != 0:
                last_err = (out.stderr or "")[-300:]
                continue
            info = json.loads(out.stdout)
            heights = [f.get("height") or 0 for f in info.get("formats", [])
                       if f.get("url") and f.get("vcodec") not in (None, "none")]
            if not heights:
                last_err = "no playable formats"
                continue
            top = max(heights)
            if best is None or top > best[0]:
                best = (top, info)
            if top >= 720:
                break
        except Exception as e:
            last_err = str(e)
            continue
    if best is None:
        raise RuntimeError(last_err or "extract failed")
    return best[1]

def get_streams(video_id: str, base: str = "") -> dict:
    now = time.time()
    if video_id in CACHE and now - CACHE[video_id][0] < 1800:
        return CACHE[video_id][1]
    info = fetch_info(video_id)
    progressive, adaptive_video, adaptive_audio = [], [], []
    hls = dash = ""
    for f in info.get("formats", []):
        url = f.get("url") or ""
        if not url:
            continue
        if f.get("protocol") == "m3u8_native" or url.endswith(".m3u8"):
            hls = url; continue
        if "manifest/dash" in url:
            dash = url; continue
        item = {
            "url": base + "/playback?u=" + urllib.parse.quote(url, safe=""),
            "mime": "video/webm" if f.get("ext") == "webm" else "video/mp4",
            "quality": f"{f.get('height')}p" if f.get("height") else (f.get("format_note") or ""),
            "height": f.get("height") or 0,
            "bitrate": int((f.get("vbr") or 0) + (f.get("abr") or 0)) * 1000,
            "fps": f.get("fps") or 0,
        }
        has_v, has_a = f.get("vcodec") not in (None, "none"), f.get("acodec") not in (None, "none")
        if has_v and has_a:
            progressive.append(item)
        elif has_v:
            adaptive_video.append(item)
        elif has_a:
            adaptive_audio.append(item)
    progressive.sort(key=lambda x: -x["height"])
    payload = {"hls": hls, "dash": dash, "progressive": progressive,
               "adaptiveVideo": adaptive_video, "adaptiveAudio": adaptive_audio,
               "durationSeconds": info.get("duration") or 0, "isLive": bool(info.get("is_live"))}
    CACHE[video_id] = (now, payload)
    return payload

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length")
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/" or u.path == "/health":
                body = b"GAS Tube proxy OK"
                self.send_response(200); self._cors()
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body); return
            if u.path.startswith("/streams/"):
                vid = re.sub(r"[^A-Za-z0-9_-]", "", u.path.split("/")[-1])[:11]
                proto = self.headers.get("X-Forwarded-Proto", "https" if self.headers.get("X-Forwarded-Ssl")=="on" else "http")
                base = f"{proto}://{self.headers.get('Host','localhost')}"
                body = json.dumps(get_streams(vid, base)).encode()
                self.send_response(200); self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body); return
            if u.path == "/playback":
                target = urllib.parse.parse_qs(u.query).get("u", [""])[0]
                if "googlevideo.com" not in target and "youtube.com" not in target:
                    self.send_response(400); self.end_headers(); return
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
                if self.headers.get("Range"): headers["Range"] = self.headers["Range"]
                r = requests.get(target, headers=headers, stream=True, timeout=30)
                self.send_response(r.status_code); self._cors()
                for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if r.headers.get(k): self.send_header(k, r.headers[k])
                self.end_headers()
                for chunk in r.iter_content(262144):
                    if chunk: self.wfile.write(chunk)
                return
            self.send_response(404); self.end_headers()
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            self.send_response(500); self._cors()
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(body)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    print(f"listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
