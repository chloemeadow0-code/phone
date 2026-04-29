import http.server
import subprocess
import os
import urllib.parse

SCREENSHOT_PATH = "screen.png"
ADB = r"D:\platform-tools\adb"

def adb(cmd):
    subprocess.run(f'{ADB} {cmd}', shell=True, capture_output=True)

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        
        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"""
<html><head><title>Silas Phone Control</title></head>
<body style='margin:0;background:#111;color:#fff;text-align:center'>
<h2>Silas Phone Control</h2>
<div style='position:relative;display:inline-block'>
< img id='screen' src='/screen' onclick='tap(event)' style='max-height:80vh;cursor:pointer'>
</div>
<br>
<button onclick="home()">Home</button>
<button onclick="back()">Back</button>
<button onclick="refresh()">Refresh</button>
<script>
function tap(e){
  var img=document.getElementById('screen');
  var r=img.getBoundingClientRect();
  var x=Math.round((e.clientX-r.left)/r.width*1080);
  var y=Math.round((e.clientY-r.top)/r.height*2400);
  fetch('/tap?x='+x+'&y='+y).then(()=>setTimeout(refresh,800));
}
function home(){fetch('/cmd?c=shell+input+keyevent+3').then(()=>setTimeout(refresh,800));}
function back(){fetch('/cmd?c=shell+input+keyevent+4').then(()=>setTimeout(refresh,800));}
function refresh(){document.getElementById('screen').src='/screen?'+Date.now();}
setInterval(refresh,3000);
</script>
</body></html>
""")
        
        elif parsed.path == "/screen":
            adb("shell screencap -p /sdcard/screen.png")
            adb(f"pull /sdcard/screen.png {SCREENSHOT_PATH}")
            if os.path.exists(SCREENSHOT_PATH):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                with open(SCREENSHOT_PATH, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
        
        elif parsed.path == "/tap":
            params = urllib.parse.parse_qs(parsed.query)
            x = params.get("x", ["0"])[0]
            y = params.get("y", ["0"])[0]
            adb(f"shell input tap {x} {y}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        
        elif parsed.path == "/cmd":
            params = urllib.parse.parse_qs(parsed.query)
            c = params.get("c", [""])[0]
            adb(c)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        
        else:
            self.send_response(404)
            self.end_headers()

print("Starting Phone Control Server on http://localhost:9999")
http.server.HTTPServer(("0.0.0.0", 9999), Handler).serve_forever()