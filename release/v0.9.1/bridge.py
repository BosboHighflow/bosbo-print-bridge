import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import sqlite3, time, threading, queue, os, subprocess, sys, json, secrets, hmac, uuid, urllib.request, urllib.error, hashlib, zipfile, re, tempfile, shutil, ctypes, csv
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('BOSBO_DATA_DIR',str(ROOT)))
DATA.mkdir(parents=True,exist_ok=True)
DB=DATA/'jobs.sqlite3'
SETTINGS=DATA/'settings.json'
TOKEN_FILE=DATA/'api_token.txt'
API_PORT=18765
API_HOST='127.0.0.1'
API_SERVER=None
VERSION='0.9.1'
MAX_BODY=4096

def load_settings():
 try:
  data=json.loads(SETTINGS.read_text(encoding='utf-8'))
  return data if isinstance(data,dict) else {}
 except (OSError,ValueError):return {}

def save_settings(data):
 tmp=SETTINGS.with_suffix('.tmp')
 tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
 tmp.replace(SETTINGS)

# Public GitHub Releases: repo is an explicit user setting, not a secret.
# We require GitHub's published SHA-256 asset digest and revalidate the ZIP
# with the installed updater before launching it. No arbitrary URL execution.
GITHUB_REPO_RE=re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
MAX_UPDATE_BYTES=8*1024*1024

def version_parts(v):
 if not isinstance(v,str) or not re.fullmatch(r'\d+(?:\.\d+){1,3}',v):raise ValueError('Gecersiz surum')
 return tuple(int(i) for i in v.split('.'))

def parse_release(release,current=VERSION):
 if not isinstance(release,dict):raise ValueError('Guncelleme verisi gecersiz')
 tag=release.get('tag_name','')
 ver=tag.lstrip('vV') if isinstance(tag,str) else ''
 if not ver or release.get('draft') or release.get('prerelease'):
  raise ValueError('Yayinlanmis kararli surum bulunamadi')
 if version_parts(ver)<=version_parts(current):return None
 assets=release.get('assets',[])
 if not isinstance(assets,list):raise ValueError('Surum dosyalari gecersiz')
 for asset in assets:
  if not isinstance(asset,dict):continue
  name=asset.get('name','')
  url=asset.get('browser_download_url','')
  digest=asset.get('digest','')
  size=asset.get('size',0)
  if (isinstance(name,str) and re.fullmatch(r'BOSBO_Print_Bridge_GUNCELLEME_V[0-9.]+\.zip',name)
      and name==f'BOSBO_Print_Bridge_GUNCELLEME_V{ver}.zip'
      and isinstance(url,str) and url.startswith('https://github.com/')
      and isinstance(digest,str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}',digest)
      and type(size) is int and 0<size<=MAX_UPDATE_BYTES):
   return {'version':ver,'name':name,'url':url,'sha256':digest.split(':')[1].lower(),'size':size}
 raise ValueError('Yeni surum bulundu ama SHA-256 dogrulamali BOSBO ZIP dosyasi yok')

def github_latest(repo):
 if not GITHUB_REPO_RE.fullmatch(repo or ''):raise ValueError('GitHub deposunu kullanici/depo biciminde gir (ornek: adim/bosbo-print-bridge)')
 url='https://api.github.com/repos/'+repo+'/releases/latest'
 req=urllib.request.Request(url,headers={'Accept':'application/vnd.github+json','User-Agent':'BOSBO-Print-Bridge/'+VERSION})
 try:
  with urllib.request.urlopen(req,timeout=12) as response:
   if response.status!=200:raise RuntimeError('GitHub yaniti: '+str(response.status))
   if int(response.headers.get('Content-Length','0'))>300_000:raise ValueError('Surum bilgisi cok buyuk')
   data=response.read(300_001)
   if len(data)>300_000:raise ValueError('Surum bilgisi cok buyuk')
   return parse_release(json.loads(data))
 except urllib.error.HTTPError as exc:
  if exc.code==404:raise RuntimeError('GitHub deposu veya yayinlanmis Releases surumu bulunamadi') from exc
  if exc.code==403:raise RuntimeError('GitHub erisim siniri veya izin hatasi (403)') from exc
  raise RuntimeError('GitHub HTTP hatasi: '+str(exc.code)) from exc

def validate_update_zip(filename,expected_version=None):
 with zipfile.ZipFile(filename) as z:
  if len(z.infolist())!=2 or set(z.namelist())!={'manifest.json','app/bridge.py'}:
   raise ValueError('Guncelleme ZIP dosya listesi gecersiz')
  if any(i.file_size>2*1024*1024 for i in z.infolist()):raise ValueError('Guncelleme dosyasi cok buyuk')
  manifest=json.loads(z.read('manifest.json'))
  if manifest.get('product')!='BOSBO_PRINT_BRIDGE':raise ValueError('Bu paket BOSBO icin degil')
  ver=manifest.get('version')
  if version_parts(ver)<=version_parts(VERSION):raise ValueError('Surum zaten kurulu veya daha eski')
  if expected_version is not None and ver!=expected_version:raise ValueError('Surum numarasi eslesmedi')
  hashes=manifest.get('files',{})
  if not isinstance(hashes,dict) or set(hashes)!={'app/bridge.py'}:raise ValueError('Manifest dosya listesi gecersiz')
  payload=z.read('app/bridge.py')
  if hashlib.sha256(payload).hexdigest()!=hashes['app/bridge.py']:raise ValueError('Dosya SHA256 dogrulamasi basarisiz')
  compile(payload,'bridge.py','exec')
 return ver

def download_verified_release(release,target_dir):
 # GitHub release assets only; release digest must match complete archive.
 from urllib.parse import urlsplit
 parts=urlsplit(release['url'])
 if parts.scheme!='https' or parts.hostname!='github.com' or not parts.path.startswith('/') or '/releases/download/' not in parts.path:
  raise ValueError('Indirme adresi GitHub Releases olmali')
 target_dir=Path(target_dir);target_dir.mkdir(parents=True,exist_ok=True)
 dest=target_dir/'bosbo_update_download.zip'
 partial=dest.with_suffix('.part')
 partial.unlink(missing_ok=True)
 try:
  req=urllib.request.Request(release['url'],headers={'User-Agent':'BOSBO-Print-Bridge/'+VERSION})
  with urllib.request.urlopen(req,timeout=25) as response,partial.open('wb') as f:
   h=hashlib.sha256();count=0
   while True:
    chunk=response.read(65536)
    if not chunk:break
    count+=len(chunk)
    if count>MAX_UPDATE_BYTES:raise ValueError('Guncelleme boyut sinirini asti')
    f.write(chunk);h.update(chunk)
  if count!=release['size']:raise ValueError('Indirilen dosya boyutu farkli')
  if h.hexdigest()!=release['sha256']:raise ValueError('Indirme SHA256 dogrulamasi basarisiz')
  partial.replace(dest)
  validate_update_zip(dest,release['version'])
  return dest
 except Exception:
  partial.unlink(missing_ok=True)
  dest.unlink(missing_ok=True)
  raise

STOP=threading.Event()
PAUSED=threading.Event()
EVENTS=queue.Queue()
_INSTANCE_MUTEX=None

def windows_app_identity():
 global _INSTANCE_MUTEX
 if os.name!='nt':return
 ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Bosbo.PrintBridge.Desktop')
 mutex=ctypes.windll.kernel32.CreateMutexW(None,False,'Local\\BosboPrintBridgeDesktopV1')
 if not mutex:raise OSError('Windows uygulama kilidi olusturulamadi')
 _INSTANCE_MUTEX=mutex
 if ctypes.windll.kernel32.GetLastError()==183:
  ctypes.windll.user32.MessageBoxW(None,'BOSBO Print Bridge zaten acik. Gorev cubugundaki mevcut pencereyi kullan.','BOSBO Print Bridge',0x40)
  sys.exit(0)


def db():
 c=sqlite3.connect(DB,timeout=10)
 c.execute('PRAGMA journal_mode=WAL')
 c.execute('CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, file TEXT, copies INTEGER, color TEXT, duplex INTEGER, status TEXT, error TEXT DEFAULT "", created TEXT)')
 c.execute('CREATE TABLE IF NOT EXISTS api_requests(request_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, created TEXT NOT NULL)')
 c.commit();return c

def add_job(path,copies,color,duplex):
 p=Path(path)
 if not p.is_file() or p.stat().st_size>50*1024*1024 or p.stat().st_size<5: raise ValueError('PDF bulunamadi veya boyut uygun degil (maks 50 MB).')
 with p.open('rb') as f:
  if f.read(5)!=b'%PDF-':raise ValueError('PDF basligi gecersiz.')
 with db() as c:
  cur=c.execute('INSERT INTO jobs(file,copies,color,duplex,status,created) VALUES(?,?,?,?,?,?)',(str(p),copies,color,int(duplex),'BEKLIYOR',time.strftime('%Y-%m-%d %H:%M:%S')))
  return cur.lastrowid

def add_api_job(path,copies,color,duplex,request_id=None):
 # All API requests are simulated; no access to a real printer occurs here.
 if request_id is not None:
  if not isinstance(request_id,str) or not 8<=len(request_id)<=100 or not all(ch.isalnum() or ch in '-_.' for ch in request_id):
   raise ValueError('request_id 8-100 karakter olmali; yalnizca harf, rakam, - _ .')
 p=Path(path)
 if not p.is_file() or not 5<=p.stat().st_size<=50*1024*1024:
  raise ValueError('PDF bulunamadi veya boyut uygun degil (maks 50 MB).')
 with p.open('rb') as f:
  if f.read(5)!=b'%PDF-':raise ValueError('PDF basligi gecersiz.')
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  if request_id is not None:
   found=c.execute('SELECT job_id FROM api_requests WHERE request_id=?',(request_id,)).fetchone()
   if found:return found[0],False
  cur=c.execute('INSERT INTO jobs(file,copies,color,duplex,status,created) VALUES(?,?,?,?,?,?)',(str(p),copies,color,int(duplex),'BEKLIYOR',time.strftime('%Y-%m-%d %H:%M:%S')))
  if request_id is not None:
   c.execute('INSERT INTO api_requests(request_id,job_id,created) VALUES(?,?,?)',(request_id,cur.lastrowid,time.strftime('%Y-%m-%d %H:%M:%S')))
  return cur.lastrowid,True

def cancel_api_job(jid):
 with db() as c:
  cur=c.execute("UPDATE jobs SET status='IPTAL_EDILDI' WHERE id=? AND status='BEKLIYOR'",(jid,))
  if cur.rowcount:return True
  exists=c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()
  if not exists:raise LookupError('Is bulunamadi')
  raise ValueError('Yalnizca BEKLIYOR durumundaki is iptal edilebilir')

def worker():
 while not STOP.is_set():
  if PAUSED.is_set():
   STOP.wait(.25);continue
  with db() as c:
   row=c.execute("SELECT id FROM jobs WHERE status='BEKLIYOR' ORDER BY id LIMIT 1").fetchone()
   if row:c.execute("UPDATE jobs SET status='ISLENIYOR' WHERE id=? AND status='BEKLIYOR'",row)
  if not row:
   STOP.wait(.25);continue
  jid=row[0]
  for _ in range(12):
   if STOP.wait(.25):break
  with db() as c:
   status=c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()
   if status and status[0]=='ISLENIYOR':
    c.execute('UPDATE jobs SET status=? WHERE id=?',('BEKLIYOR' if STOP.is_set() or PAUSED.is_set() else 'SIMULE_EDILDI',jid))
  EVENTS.put('refresh')

def get_token():
 if TOKEN_FILE.exists():
  return TOKEN_FILE.read_text(encoding='ascii').strip()
 token=secrets.token_urlsafe(32)
 with TOKEN_FILE.open('x',encoding='ascii') as f:f.write(token)
 try:os.chmod(TOKEN_FILE,0o600)
 except OSError:pass
 return token

class APIHandler(BaseHTTPRequestHandler):
 def log_message(self,format,*args):pass
 def respond(self,status,payload):
  body=json.dumps(payload,ensure_ascii=False).encode('utf-8')
  self.send_response(status)
  self.send_header('Content-Type','application/json; charset=utf-8')
  self.send_header('Content-Length',str(len(body)))
  self.send_header('Cache-Control','no-store')
  self.end_headers();self.wfile.write(body)
 def authorized(self):
  token=self.headers.get('X-Bosbo-Token','')
  return hmac.compare_digest(token,get_token())
 def do_GET(self):
  if not self.authorized():return self.respond(401,{'error':'Yetkisiz istek'})
  if self.path=='/health':return self.respond(200,{'service':'stopped' if STOP.is_set() else ('paused' if PAUSED.is_set() else 'running'),'mode':'SIMULASYON','real_print':False,'version':VERSION})
  if self.path=='/jobs':
   with db() as c:rows=c.execute('SELECT id,file,copies,color,duplex,status,error,created FROM jobs ORDER BY id DESC LIMIT 100').fetchall()
   return self.respond(200,{'jobs':[dict(zip(('id','file','copies','color','duplex','status','error','created'),r)) for r in rows]})
  return self.respond(404,{'error':'Bilinmeyen yol'})
 def do_POST(self):
  if not self.authorized():return self.respond(401,{'error':'Yetkisiz istek'})
  if STOP.is_set() or PAUSED.is_set():return self.respond(409,{'error':'Servis durdurulmus veya duraklatilmis'})
  if self.path.startswith('/jobs/') and self.path.endswith('/cancel'):
   parts=self.path.split('/')
   if len(parts)!=4 or not parts[2].isdigit():return self.respond(404,{'error':'Bilinmeyen yol'})
   try:
    cancel_api_job(int(parts[2]));return self.respond(200,{'job_id':int(parts[2]),'status':'IPTAL_EDILDI'})
   except LookupError as exc:return self.respond(404,{'error':str(exc)})
   except ValueError as exc:return self.respond(409,{'error':str(exc)})
  if self.path!='/jobs':return self.respond(404,{'error':'Bilinmeyen yol'})
  try:
   length=int(self.headers.get('Content-Length','0'))
   if not 1<=length<=MAX_BODY:raise ValueError('Istek boyutu uygun degil')
   payload=json.loads(self.rfile.read(length))
   if not isinstance(payload,dict):raise ValueError('JSON nesnesi bekleniyor')
   path=payload.get('file','');copies=payload.get('copies',1);color=payload.get('color','Siyah-beyaz');duplex=payload.get('duplex',False)
   if not isinstance(path,str) or not path or len(path)>1024:raise ValueError('Dosya yolu gecersiz')
   if type(copies) is not int or not 1<=copies<=20:raise ValueError('Kopya 1-20 olmali')
   if color not in ('Renkli','Siyah-beyaz'):raise ValueError('Renk secimi gecersiz')
   if type(duplex) is not bool:raise ValueError('Cift tarafli true/false olmali')
   jid,created=add_api_job(path,copies,color,duplex,payload.get('request_id'))
   EVENTS.put('refresh')
   return self.respond(201 if created else 200,{'job_id':jid,'status':'BEKLIYOR' if created else 'MEVCUT','created':created,'mode':'SIMULASYON'})
  except (ValueError,OSError,TypeError,json.JSONDecodeError) as exc:return self.respond(400,{'error':str(exc)})

def start_api():
 global API_SERVER
 get_token()
 try:
  API_SERVER=ThreadingHTTPServer((API_HOST,API_PORT),APIHandler)
  API_SERVER.serve_forever(poll_interval=.2)
 except OSError as exc:
  EVENTS.put('API HATASI: '+str(exc))

class App:
 def __init__(self,root):
  self.root=root;root.title('BOSBO Print Bridge V'+VERSION+' - SIMULASYON');root.geometry('1030x690')
  self.file=tk.StringVar();self.copies=tk.IntVar(value=1);self.color=tk.StringVar(value='Siyah-beyaz');self.duplex=tk.BooleanVar(value=True)
  self.status=tk.StringVar(value='Servis: AKTIF | Gercek baski: KAPALI')
  self.settings=load_settings()
  self.printer_choice=tk.StringVar(value=self.settings.get('printer','(Secilmedi)'))
  self.copies.set(self.settings.get('copies',1))
  self.color.set(self.settings.get('color','Siyah-beyaz'))
  self.duplex.set(self.settings.get('duplex',True))
  tabs=ttk.Notebook(root);tabs.pack(fill='both',expand=True,padx=12,pady=12)
  main=ttk.Frame(tabs,padding=12);printers=ttk.Frame(tabs,padding=12);tabs.add(main,text='Baski ve kuyruk');tabs.add(printers,text='Yazicilar');updates=ttk.Frame(tabs,padding=16);tabs.add(updates,text='Guncellemeler');automation=ttk.Frame(tabs,padding=16);tabs.add(automation,text='Otomasyon merkezi')
  ttk.Label(main,text='BOSBO PRINT BRIDGE V'+VERSION,font=('Segoe UI',17,'bold')).pack(anchor='w')
  ttk.Label(main,text='SAHTE YAZICI - gercek baski kesinlikle kapali').pack(anchor='w',pady=(0,10))
  line=ttk.Frame(main);line.pack(fill='x');ttk.Entry(line,textvariable=self.file).pack(side='left',fill='x',expand=True);ttk.Button(line,text='PDF Sec',command=self.choose).pack(side='left',padx=6)
  opts=ttk.Frame(main);opts.pack(anchor='w',pady=10);ttk.Label(opts,text='Kopya').pack(side='left');tk.Spinbox(opts,from_=1,to=20,textvariable=self.copies,width=4).pack(side='left',padx=6)
  ttk.Combobox(opts,textvariable=self.color,values=['Siyah-beyaz','Renkli'],state='readonly',width=16).pack(side='left',padx=8);ttk.Checkbutton(opts,text='Cift tarafli',variable=self.duplex).pack(side='left')
  actions=ttk.Frame(main);actions.pack(fill='x',pady=5)
  for label,fn in [('Simule et',self.submit),('Yenile',self.refresh),('Secileni iptal et',self.cancel),('Yeniden dene',self.retry),('Servisi durdur / baslat',self.toggle),('Gecmisi CSV aktar',self.export_history)]:ttk.Button(actions,text=label,command=fn).pack(side='left',padx=(0,6))
  ttk.Label(main,textvariable=self.status).pack(anchor='w',pady=8)
  self.queue_summary=tk.StringVar(value='Kuyruk bilgisi yukleniyor...')
  ttk.Label(main,textvariable=self.queue_summary).pack(anchor='w',pady=(0,5))
  ttk.Label(main,text='Yerel API: 127.0.0.1:18765 | Token: api_token.txt | Gercek baski KAPALI').pack(anchor='w')
  cols=('id','file','copies','color','duplex','status','error')
  self.table=ttk.Treeview(main,columns=cols,show='headings',selectmode='browse')
  for key,title,width in [('id','#',45),('file','Dosya',260),('copies','Kopya',65),('color','Renk',100),('duplex','Cift',65),('status','Durum',125),('error','Hata',180)]:self.table.heading(key,text=title);self.table.column(key,width=width,stretch=key in ('file','error'))
  self.table.pack(fill='both',expand=True)
  ttk.Label(printers,text='Windows yazicilari (salt okunur, gercek baski kapali)',font=('Segoe UI',12,'bold')).pack(anchor='w')
  ttk.Button(printers,text='Yazicilari tara',command=self.scan).pack(anchor='w',pady=10)
  ttk.Label(printers,text='Secilen yazici (sadece kaydedilir, baski GONDERILMEZ)').pack(anchor='w')
  self.printer_combo=ttk.Combobox(printers,textvariable=self.printer_choice,state='readonly',width=65)
  self.printer_combo.pack(anchor='w',pady=6)
  ttk.Button(printers,text='Yazici ve baski tercihlerini kaydet',command=self.save_preferences).pack(anchor='w',pady=4)
  self.default_label=ttk.Label(printers,text='Varsayilan yazici: taraniyor...')
  self.default_label.pack(anchor='w',pady=5)
  self.printer_list=tk.Listbox(printers);self.printer_list.pack(fill='both',expand=True)
  ttk.Label(updates,text='BOSBO Print Bridge - Guncellemeler',font=('Segoe UI',16,'bold')).pack(anchor='w',pady=(0,12))
  ttk.Label(updates,text='Kurulu surum: V'+VERSION+' | Gercek baski: KAPALI').pack(anchor='w',pady=4)
  ttk.Label(updates,text='GitHub Releases yayini (kullanici/depo):').pack(anchor='w',pady=(10,2))
  reporow=ttk.Frame(updates);reporow.pack(fill='x',pady=4)
  self.repo_var=tk.StringVar(value=self.settings.get('update_repo',''))
  ttk.Entry(reporow,textvariable=self.repo_var,width=48).pack(side='left',padx=(0,8))
  ttk.Button(reporow,text='Depoyu kaydet',command=self.save_update_repo).pack(side='left')
  actions_update=ttk.Frame(updates);actions_update.pack(anchor='w',pady=10)
  ttk.Button(actions_update,text='Guncelleme ara',command=self.check_online_update).pack(side='left',padx=(0,8))
  self.download_btn=ttk.Button(actions_update,text='Bulunan guncellemeyi indir ve kur',command=self.install_online_update,state='disabled')
  self.download_btn.pack(side='left')
  self.update_info=tk.StringVar(value='Yayin deposu tanimlanmadi.' if not self.repo_var.get() else 'Guncelleme aramaya hazir.')
  ttk.Label(updates,textvariable=self.update_info,wraplength=840).pack(anchor='w',pady=(0,12))
  self.pending_release=None
  ttk.Separator(updates).pack(fill='x',pady=8)
  ttk.Label(updates,text='Elle guncelleme (cevrimdisi/kurtarma):').pack(anchor='w',pady=4)
  ttk.Button(updates,text='Yerel guncelleme ZIP sec',command=self.install_update).pack(anchor='w',pady=5)
  ttk.Label(updates,text='Veriler ve API anahtari ayridir. Indirilen paket SHA-256 ile dogrulanir.',wraplength=840).pack(anchor='w',pady=8)
  ttk.Label(updates,text='Not: Once yeni surumu GitHub Releases uzerinde yayinlamalisin; yayin yoksa otomatik indirme yapilamaz.',wraplength=840).pack(anchor='w')
  ttk.Label(automation,text='BOSBO - Otomasyon Merkezi',font=('Segoe UI',16,'bold')).pack(anchor='w',pady=(0,12))
  ttk.Label(automation,text='Yerel komut koprusu | Yalnizca bu bilgisayar | Gercek baski KAPALI').pack(anchor='w',pady=4)
  ttk.Label(automation,text='Bu merkez API ile PDF gondermeyi gercekten test eder; ChatGPT baglantisi henuz kurulmus degildir.',wraplength=820).pack(anchor='w',pady=4)
  ttk.Separator(automation).pack(fill='x',pady=12)
  ttk.Label(automation,text='Baglanti ve komut testleri',font=('Segoe UI',11,'bold')).pack(anchor='w',pady=5)
  testactions=ttk.Frame(automation);testactions.pack(anchor='w',pady=5)
  ttk.Button(testactions,text='API saglik testi',command=self.api_health_test).pack(side='left',padx=(0,8))
  ttk.Button(testactions,text='Secili PDF ile API test isi olustur',command=self.api_job_test).pack(side='left',padx=(0,8))
  ttk.Button(testactions,text='API is listesini denetle',command=self.api_jobs_test).pack(side='left')
  ttk.Button(testactions,text='Sistem tanilama',command=self.diagnostics).pack(side='left',padx=8)
  ttk.Label(automation,text='API testi icin Baski ve kuyruk sekmesinde PDF ve baski tercihlerini sec.').pack(anchor='w',pady=(10,4))
  self.api_log=tk.Text(automation,height=13,state='disabled',wrap='word')
  self.api_log.pack(fill='both',expand=True,pady=8)
  self.log_api('Hazir. Komutlar yalnizca SIMULASYON kuyruguna eklenir.')
  ttk.Label(automation,text='Guvenlik: API anahtarini paylasma. Internetten erisim acma. Gercek baski bu surumde yapilmaz.',wraplength=900).pack(anchor='w',pady=4)
  self.root.protocol('WM_DELETE_WINDOW',self.hide)
  self.refresh();self.scan();self.root.after(500,self.tick)
  self.root.after(1100,self.show_update_notice)
  if self.repo_var.get():self.root.after(3500,lambda:self.check_online_update(silent=True))
 def show_update_notice(self):
  status_file=Path(os.environ.get('BOSBO_DATA_DIR',str(ROOT))).parent/'update_status.json'
  try:
   if not status_file.is_file():return
   report=json.loads(status_file.read_text(encoding='utf-8'))
   status_file.unlink(missing_ok=True)
   if report.get('status')=='success':
    messagebox.showinfo('Guncelleme tamamlandi',f"BOSBO Print Bridge V{report.get('version',VERSION)} basariyla kuruldu.\nAyarlar ve baski gecmisi korundu.")
   else:
    messagebox.showerror('Guncelleme basarisiz',f"Kurulum tamamlanamadi.\nNeden: {report.get('error','Bilinmeyen hata')}\n\nAyrinti: update_log.txt")
  except (OSError,ValueError) as exc:
   self.log_api('Guncelleme sonuc dosyasi okunamadi: '+str(exc))
 def log_api(self,message):
  self.api_log.config(state='normal')
  self.api_log.insert('end',time.strftime('%H:%M:%S')+'  '+str(message)+'\n')
  self.api_log.see('end')
  self.api_log.config(state='disabled')
 def api_request_async(self,method,route,payload=None):
  def work():
   try:
    body=json.dumps(payload,ensure_ascii=False).encode('utf-8') if payload is not None else None
    request=urllib.request.Request(f'http://127.0.0.1:{API_PORT}'+route,data=body,method=method,headers={'X-Bosbo-Token':get_token(),'Content-Type':'application/json'})
    try:
     with urllib.request.urlopen(request,timeout=5) as response:
      result={'http':response.status,'result':json.load(response)}
    except urllib.error.HTTPError as exc:
     result={'http':exc.code,'result':json.loads(exc.read().decode('utf-8'))}
    self.root.after(0,lambda:self.show_api_result(result))
   except Exception as exc:
    message=str(exc)
    self.root.after(0,lambda:self.log_api('HATA: '+message))
  threading.Thread(target=work,daemon=True).start()
 def show_api_result(self,result):
  self.log_api(json.dumps(result,ensure_ascii=False))
  self.refresh()
 def api_health_test(self):self.api_request_async('GET','/health')
 def diagnostics(self):
  try:
   with db() as c:
    jobs=c.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status').fetchall()
    integrity=c.execute('PRAGMA quick_check').fetchone()[0]
   report={'version':VERSION,'print_mode':'SIMULASYON','real_print':False,'api_address':f'{API_HOST}:{API_PORT}','api_paused':PAUSED.is_set(),'database_integrity':integrity,'jobs_by_status':dict(jobs),'update_repo':self.repo_var.get().strip()}
   self.log_api('TANILAMA: '+json.dumps(report,ensure_ascii=False))
  except Exception as exc:self.log_api('TANILAMA HATASI: '+str(exc))
 def api_jobs_test(self):
  # The UI shows the first jobs only, never token contents.
  self.api_request_async('GET','/jobs')
 def api_job_test(self):
  path=self.file.get()
  try:
   n=int(self.copies.get())
   if not 1<=n<=20:raise ValueError('Kopya 1-20 olmali')
   if not Path(path).is_file():raise ValueError('Once Baski ve kuyruk sekmesinden PDF sec')
   if not messagebox.askyesno('API simule baski',f'Yerel API ile SIMULASYON isine eklensin mi?\n{Path(path).name}\n{n} kopya - {self.color.get()}\n\nGercek baski KAPALI.'):
    return
   payload={'file':path,'copies':n,'color':self.color.get(),'duplex':self.duplex.get(),'request_id':uuid.uuid4().hex}
   self.api_request_async('POST','/jobs',payload)
  except (ValueError,OSError) as exc:messagebox.showerror('API test hatasi',str(exc))
 def save_update_repo(self):
  repo=self.repo_var.get().strip()
  if repo and not GITHUB_REPO_RE.fullmatch(repo):
   messagebox.showerror('GitHub depo hatasi','Depoyu kullanici/depo biciminde gir.');return
  try:
   settings=load_settings();settings['update_repo']=repo;save_settings(settings);self.settings=settings
   self.pending_release=None;self.download_btn.config(state='disabled')
   self.update_info.set('Depo kaydedildi. Guncelleme ara butonuna bas.' if repo else 'Yayin deposu tanimlanmadi.')
  except OSError as exc:messagebox.showerror('Kaydetme hatasi',str(exc))
 def check_online_update(self,silent=False):
  repo=self.repo_var.get().strip()
  if not repo:
   if not silent:messagebox.showinfo('Yayin deposu gerekli','Once GitHub kullanici/depo adini kaydet. Yayinlanan bir Releases surumu olmasi gerekir.')
   return
  if not GITHUB_REPO_RE.fullmatch(repo):
   if not silent:messagebox.showerror('Depo gecersiz','GitHub kullanici/depo biciminde olmali.')
   return
  self.pending_release=None;self.download_btn.config(state='disabled')
  self.update_info.set('GitHub Releases denetleniyor...')
  def work():
   try:
    release=github_latest(repo)
    self.root.after(0,lambda:self.finish_update_check(release))
   except Exception as exc:
    message=str(exc);self.root.after(0,lambda:self.update_info.set('Kontrol edilemedi: '+message))
  threading.Thread(target=work,daemon=True).start()
 def finish_update_check(self,release):
  if release is None:
   self.update_info.set('Guncelsin! Yeni kararli surum bulunmadi.');return
  self.pending_release=release
  self.update_info.set('Yeni surum: V'+release['version']+' | SHA-256 bilgisi mevcut | '+release['name'])
  self.download_btn.config(state='normal')
 def install_online_update(self):
  release=self.pending_release
  if not release:return
  if not messagebox.askyesno('Guncelleme onayi','GitHub uzerindeki V'+release['version']+' surumu indirilip kurulacak.\n\nUygulama kapanip yeniden acilacak.\nAyarlar ve gecmis korunacak.'):return
  self.download_btn.config(state='disabled')
  self.update_info.set('Indiriliyor ve SHA-256 dogrulaniyor...')
  def work():
   try:
    # Save under app installation root, not a temporary directory that may be removed.
    updater=Path(os.environ.get('BOSBO_UPDATER',''))
    if not updater.is_file():raise ValueError('Kalici kurulumdaki guncelleyici bulunamadi')
    path=download_verified_release(release,updater.parent/'update_cache')
    self.root.after(0,lambda:self.launch_update(path,release['version'],already_confirmed=True))
   except Exception as exc:
    msg=str(exc)
    def failure():
     self.update_info.set('Indirme/denetim basarisiz: '+msg)
     self.download_btn.config(state='normal')
    self.root.after(0,failure)
  threading.Thread(target=work,daemon=True).start()
 def launch_update(self,path,version,already_confirmed=False):
  try:
   validate_update_zip(path,version)
   if not already_confirmed and not messagebox.askyesno('Guncelleme onayi','V'+version+' kurulsun mu? Program yeniden baslatilacak.'):
    return
   updater=Path(os.environ.get('BOSBO_UPDATER',''))
   if not updater.is_file():raise ValueError('Kalici kurulum guncelleyicisi bulunamadi')
   subprocess.Popen([sys.executable,str(updater),'--package',str(path),'--parent-pid',str(os.getpid())],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True)
   STOP.set();self.root.destroy()
  except Exception as exc:messagebox.showerror('Guncelleme hatasi',str(exc))
 def install_update(self):
  path=filedialog.askopenfilename(title='BOSBO guncelleme paketi',filetypes=[('ZIP paketi','*.zip')])
  if not path:return
  try:
   version=validate_update_zip(path)
   self.launch_update(path,version)
  except Exception as exc:messagebox.showerror('Paket hatasi',str(exc))
 def choose(self):
  p=filedialog.askopenfilename(filetypes=[('PDF','*.pdf')]);
  if p:self.file.set(p)
 def submit(self):
  if STOP.is_set() or PAUSED.is_set():messagebox.showwarning('Servis durdu','Once servisi baslat.');return
  try:
   n=int(self.copies.get());assert 1<=n<=20
   
   if not messagebox.askyesno('Simulasyon onayi',f'Dosya: {Path(self.file.get()).name}\nKopya: {n}\nRenk: {self.color.get()}\nCift tarafli: {"Evet" if self.duplex.get() else "Hayir"}\n\nSADECE SIMULASYON - kagit basilmaz.'):return
   self.save_preferences(silent=True)
   add_job(self.file.get(),n,self.color.get(),self.duplex.get());self.refresh()
  except (ValueError,AssertionError,OSError) as e:messagebox.showerror('Hata',str(e) or 'Kopya 1-20 olmali')
 def selected(self):
  s=self.table.selection();return int(s[0]) if s else None
 def cancel(self):
  jid=self.selected()
  if jid is None:return
  with db() as c:
   status=c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()
   if status and status[0]=='BEKLIYOR':c.execute("UPDATE jobs SET status='IPTAL_EDILDI' WHERE id=?",(jid,))
   else:messagebox.showinfo('Bilgi','Yalnizca BEKLIYOR durumundaki is iptal edilebilir.')
  self.refresh()
 def retry(self):
  jid=self.selected()
  if jid is None:return
  if STOP.is_set() or PAUSED.is_set():messagebox.showwarning('Servis durdu','Once servisi baslat.');return
  with db() as c:
   status=c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()
   if status and status[0] in ('IPTAL_EDILDI','HATA','SIMULE_EDILDI'):c.execute("UPDATE jobs SET status='BEKLIYOR',error='' WHERE id=?",(jid,))
   else:messagebox.showinfo('Bilgi','Bu is yeniden denenemez.')
  self.refresh()
 def toggle(self):
  if PAUSED.is_set():PAUSED.clear()
  else:PAUSED.set()
  self.status.set('Servis: '+('DURAKLATILDI' if PAUSED.is_set() else 'AKTIF')+' | Gercek baski: KAPALI')
 def export_history(self):
  path=filedialog.asksaveasfilename(title='BOSBO baski gecmisini aktar',defaultextension='.csv',filetypes=[('CSV dosyasi','*.csv')],initialfile='BOSBO_Baski_Gecmisi.csv')
  if not path:return
  try:
   with db() as c:
    records=c.execute('SELECT id,file,copies,color,duplex,status,error,created FROM jobs ORDER BY id DESC').fetchall()
   with open(path,'w',newline='',encoding='utf-8-sig') as out:
    writer=csv.writer(out)
    writer.writerow(['ID','Dosya','Kopya','Renk','Cift tarafli','Durum','Hata','Olusturulma'])
    writer.writerows(records)
   messagebox.showinfo('Aktarildi',f'{len(records)} kayit CSV dosyasina aktarildi. Dosya: {path}')
  except Exception as exc:messagebox.showerror('Aktarim hatasi',str(exc))
 def refresh(self):
  chosen=self.selected();current={str(i):tuple(self.table.item(i,'values')) for i in self.table.get_children()}
  with db() as c:rows=c.execute('SELECT id,file,copies,color,duplex,status,error FROM jobs ORDER BY id DESC LIMIT 200').fetchall()
  counts={}
  for item in rows:counts[item[5]]=counts.get(item[5],0)+1
  self.queue_summary.set('Son 200 is | Bekleyen: '+str(counts.get('BEKLIYOR',0))+' | Islenen: '+str(counts.get('ISLENIYOR',0))+' | Simule edilen: '+str(counts.get('SIMULE_EDILDI',0))+' | Iptal: '+str(counts.get('IPTAL_EDILDI',0))+' | Hata: '+str(counts.get('HATA',0)))
  ids=set()
  for jid,path,n,color,duplex,status,error in rows:
   key=str(jid);ids.add(key);v=(jid,Path(path).name,n,color,'Evet' if duplex else 'Hayir',status,error)
   if key not in current:self.table.insert('', 'end',iid=key,values=v)
   elif current[key]!=tuple(map(str,v)):self.table.item(key,values=v)
  for key in current:
   if key not in ids:self.table.delete(key)
  if chosen is not None and str(chosen) in ids:self.table.selection_set(str(chosen))
 def save_preferences(self,silent=False):
  try:
   n=int(self.copies.get())
   if not 1<=n<=20:raise ValueError('Kopya 1-20 olmali')
   settings=load_settings();settings.update({'printer':self.printer_choice.get(),'copies':n,'color':self.color.get(),'duplex':bool(self.duplex.get()),'real_print_enabled':False});save_settings(settings)
   if not silent:messagebox.showinfo('Kaydedildi','Tercihler kaydedildi. Gercek baski KAPALI.')
  except (OSError,ValueError) as e:messagebox.showerror('Ayar hatasi',str(e))
 def scan(self):
  self.printer_list.delete(0,'end')
  if os.name!='nt':
   self.printer_list.insert('end','Windows disinda yazici tarama kullanilamaz')
   self.printer_combo['values']=['(Secilmedi)']
   self.default_label.config(text='Varsayilan yazici: Windows disi')
   return
  try:
   ps="Get-CimInstance Win32_Printer | Select-Object Name,Default | ConvertTo-Json -Compress"
   p=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',ps],capture_output=True,text=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
   if p.returncode:raise RuntimeError(p.stderr[:200] or 'PowerShell tarama hatasi')
   result=json.loads(p.stdout) if p.stdout.strip() else []
   items=result if isinstance(result,list) else [result]
   names=[];default='(Bulunamadi)'
   for item in items:
    name=item.get('Name','').strip()
    if not name:continue
    names.append(name)
    self.printer_list.insert('end',name+(' [VARSAYILAN]' if item.get('Default') else ''))
    if item.get('Default'):default=name
   self.printer_combo['values']=['(Secilmedi)']+names
   if self.printer_choice.get() not in names:self.printer_choice.set('(Secilmedi)')
   self.default_label.config(text='Varsayilan yazici: '+default)
   if not names:self.printer_list.insert('end','Windows yazicisi bulunamadi')
  except Exception as e:
   self.printer_list.insert('end','Tarama hatasi: '+str(e))
   self.default_label.config(text='Varsayilan yazici: okunamadi')
 def hide(self):
  # Keep the window recoverable via taskbar when the user chooses not to exit.
  if messagebox.askyesno('BOSBO Print Bridge','Program tamamen kapatilsin mi?\n\nEvet: uygulamayi ve yerel API servisini kapat.\nHayir: gorev cubuguna kucult ve calismaya devam et.',parent=self.root):
   STOP.set()
   self.root.destroy()
  else:self.root.iconify()
 def tick(self):
  try:
   while True:EVENTS.get_nowait()
  except queue.Empty:pass
  self.refresh();self.root.after(1000,self.tick)

if __name__=='__main__':
 windows_app_identity()
 db().close()
 threading.Thread(target=worker,daemon=True).start()
 threading.Thread(target=start_api,daemon=True).start()
 root=tk.Tk();App(root);root.mainloop()
