"""현황 페이지를 비밀번호로 잠가서 GitHub Pages 에 올릴 index.html 을 만든다.
사용: PAGE_PASSWORD=비번 python encrypt_page.py data/입찰현황.html _site/index.html
페이지 내용은 AES-GCM 으로 암호화되고, 브라우저에서 비밀번호를 넣어야 풀린다."""
import base64, os, sys
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITER = 250_000

src, dst = sys.argv[1], sys.argv[2]
pw = os.environ["PAGE_PASSWORD"].encode("utf-8")
html = open(src, encoding="utf-8").read().encode("utf-8")
salt, iv = os.urandom(16), os.urandom(12)
key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(pw)
ct = AESGCM(key).encrypt(iv, html, None)
b = lambda x: base64.b64encode(x).decode()

page = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>구내식당 입찰현황</title>
<style>
body{margin:0;font-family:-apple-system,"Malgun Gothic",sans-serif;background:#f4f5f7;color:#222;display:flex;min-height:100vh;align-items:center;justify-content:center}
form{background:#fff;padding:28px 24px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);width:min(320px,calc(100vw - 32px));box-sizing:border-box}
h1{font-size:18px;margin:0 0 16px}input{width:100%;box-sizing:border-box;padding:10px;font-size:16px;border:1px solid #ccc;border-radius:8px}
button{margin-top:12px;width:100%;padding:10px;font-size:16px;border:0;border-radius:8px;background:#1f6feb;color:#fff;cursor:pointer}
label{display:flex;gap:6px;align-items:center;margin-top:10px;font-size:14px;color:#555}label input{width:auto}
#msg{color:#c00;font-size:14px;min-height:18px;margin-top:8px}
</style></head><body>
<form id="f"><h1>구내식당 입찰현황</h1>
<input id="pw" type="password" placeholder="비밀번호" autocomplete="current-password" autofocus>
<label><input id="keep" type="checkbox" checked>이 기기에서 기억하기</label>
<button>열기</button><div id="msg"></div></form>
<script>
const D={salt:"%SALT%",iv:"%IV%",ct:"%CT%",iter:%ITER%};
const u=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
async function openPage(pw){
  const base=await crypto.subtle.importKey("raw",new TextEncoder().encode(pw),"PBKDF2",false,["deriveKey"]);
  const key=await crypto.subtle.deriveKey({name:"PBKDF2",salt:u(D.salt),iterations:D.iter,hash:"SHA-256"},base,{name:"AES-GCM",length:256},false,["decrypt"]);
  const buf=await crypto.subtle.decrypt({name:"AES-GCM",iv:u(D.iv)},key,u(D.ct));
  const html=new TextDecoder().decode(buf);
  document.open();document.write(html);document.close();
}
const K="bid_page_pw";
let saved=null;try{saved=localStorage.getItem(K)}catch(e){}
if(saved){openPage(saved).catch(()=>{try{localStorage.removeItem(K)}catch(e){}})}
document.getElementById("f").addEventListener("submit",async e=>{
  e.preventDefault();const pw=document.getElementById("pw").value;
  document.getElementById("msg").textContent="여는 중…";
  try{
    if(document.getElementById("keep").checked){try{localStorage.setItem(K,pw)}catch(e){}}
    await openPage(pw);
  }catch(err){
    try{localStorage.removeItem(K)}catch(e){}
    document.getElementById("msg").textContent="비밀번호가 맞지 않습니다.";
  }
});
</script></body></html>"""
page = page.replace("%SALT%", b(salt)).replace("%IV%", b(iv)).replace("%CT%", b(ct)).replace("%ITER%", str(ITER))
os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
open(dst, "w", encoding="utf-8").write(page)
print("암호화된 페이지 저장:", dst)
