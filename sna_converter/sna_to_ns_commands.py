#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""
sna_to_ns_commands.py
Cisco SNA / NetFlow CSV を入力に、Network Sketcher の CLI コマンド列と
[FLOW] 貼り付け用 CSV を出力する。Windows / macOS / Ubuntu 対応。

入力CSV形式 (自動判定):
  - API形式  : searchSubject.* / peer.* など機械可読のカラム
  - UI形式   : Subject IP Address 等 人間可読 (Total Bytes="56.49 M", Duration="36min 38s",
               Peer Port/Protocol="2055/UDP")。必要列へ自動正規化して処理。

設計(NetFlow-only前提):
  - Inside Hosts = RFC1918 + 自組織公開帯(INSIDE_PUBLIC)
  - 観測IP -> /24 サブネット(=セグメント/VLAN)に集約 (flows>=THRESH)
  - サブネットを /16 へまとめ、トラフィックグラフ(最近傍)で拠点へ集約 + spur分離
  - sna_to_ns_config.json の site_cidrs で任意CIDR->拠点を明示指定可(自動集約より優先)
  - server/client 役割は SNA orientation(peer=サーバ)で判定
  - 拠点を「DC相当(サーバ系)」「クライアント主体」に分類
  - レイアウト: (Internet-Svc) / Internet_wp_ / DC拠点(上段) / WAN_wp_ / client拠点(下段)

出力 (各入力CSVごとに Output_data/<csv名>/ 配下):
  - gen_master_commands.txt : Network Sketcher CLIコマンド列
  - gen_flow_list.csv        : [FLOW] 貼り付け用CSV (Source/Dest=マスタ機器名, Max bandwidth Mbps)
  - out_of_scope_ips.csv     : 採用されなかったサーバ候補IP(理由付き)
  - _normalized_flow.csv     : UI形式入力時の正規化中間ファイル

エンドポイント登録(RULE 11.5準拠 = SVI無し/物理ポート直IP):
  --endpoints {none,servers,clients,both}   (既定 both)
      servers : 内部サーバIPを 1IP=1デバイス。インターネット宛は(proto,port)で1デバイスに集約
      clients : 1セグメント=1 PCデバイス(IPなし)
      both    : 両方

使い方:
  python sna_to_ns_commands.py                         # Input_data 内の全CSVを一括変換
  python sna_to_ns_commands.py path/to/flow.csv        # 単一CSVを変換
  python sna_to_ns_commands.py path/to/folder          # フォルダ内の全CSVを変換
  python sna_to_ns_commands.py --config my.json --endpoints both
"""
import csv, collections, sys, json, os, argparse, ipaddress, math, re, subprocess
sys.stdout.reconfigure(encoding="utf-8")

# ================= CONFIG =================
# 既定の設定フォルダ(スクリプトと同じ)。--config で上書き可。
BASEDIR = os.path.dirname(os.path.abspath(__file__))
THRESH = 100
# --- 手動オーバーライドの既定値 (空=自動判定に委ねる)。ns_config.json で上書き、競合時は手動優先 ---
INSIDE_PUBLIC = ()                      # 自組織の公開帯 (Inside扱い)。空なら自動推定
K_CAMPUS = 2                            # クライアント拠点シード数(主要ハブ)
DC_FORCE_REGIONS = set()               # DC相当として強制する/16 (手動追加)
SPUR_REGIONS = set()                   # 独立拠点(スパー)にする/16 (手動追加)
MERGE_ALL_DC = True                     # DC系/16をひとつのDC拠点にまとめる
NAME_MAP = {}                          # シード/16 -> 拠点名 (手動ラベル, 自動名を上書き)
DC_SITE_NAME = "Datacenter"
SPUR_NAME = {}                          # spur /16 -> 拠点名 (手動。無ければ Site-<region> 自動)
CODE_MAP = {"Datacenter":"DC"}        # 拠点名 -> 短縮コード (手動。無ければ自動短縮)
EP_ROW_WIDTH = 8                        # device_location 下段の1行あたりエンドポイント数
CHUNK = 40                              # 巨大bulkコマンドの分割サイズ(MCP投入用)
# --- 自動判定の既定パラメータ (ns_config.json で上書き可) ---
AUTO_INSIDE = True                      # 公開帯Inside自動推定 ON/OFF
AUTO_DC = True                          # DC拠点自動判定 ON/OFF
AUTO_SPUR = True                        # スパー拠点自動判定 ON/OFF
SPUR_LINK_RATIO = 0.05                  # spur判定: シードとの最大結合 < ratio*内部flows なら孤立
# ==========================================

ap = argparse.ArgumentParser(
    description="Cisco SNA/NetFlow CSV -> Network Sketcher commands + [FLOW] CSV")
ap.add_argument("input", nargs="?", default=None,
                help="入力CSV(単一ファイル)またはフォルダ。省略時は --input-dir 内の全CSVを処理")
ap.add_argument("--input-dir", default="Input_data",
                help="一括処理する入力フォルダ (既定: Input_data)")
ap.add_argument("--output-dir", default="Output_data",
                help="出力ルート (既定: Output_data)。各CSVは <output-dir>/<csv名>/ に出力")
ap.add_argument("--endpoints", choices=["none","servers","clients","both"], default="both")
ap.add_argument("--server-min-flows", type=int, default=1)
ap.add_argument("--no-flow", action="store_true",
                help="[FLOW] 貼り付け用 CSV (gen_flow_list.csv) を生成しない")
ap.add_argument("--config", default=None,
                help="設定JSONのパス (既定: スクリプトと同じフォルダの sna_to_ns_config.json)")
ap.add_argument("--outdir", default=None,
                help="(内部用) 単一CSVの出力フォルダを明示指定")
args = ap.parse_args()

# ---- バッチドライバ: フォルダ/既定(Input_data)指定時は各CSVを個別プロセスで処理 ----
def _is_csv(p): return p.lower().endswith(".csv")
single_file = args.input if (args.input and os.path.isfile(args.input)) else None
if single_file is None:
    folder = args.input if (args.input and os.path.isdir(args.input)) else args.input_dir
    if not os.path.isdir(folder):
        print("[ERROR] 入力フォルダが見つかりません: %s"%os.path.abspath(folder))
        print("        CSVを置くか、単一CSVのパスを指定してください。"); sys.exit(1)
    csvs=sorted(os.path.join(folder,f) for f in os.listdir(folder)
                if _is_csv(f) and not f.startswith("_normalized_")
                and os.path.isfile(os.path.join(folder,f)))
    if not csvs:
        print("[ERROR] CSVが見つかりません: %s"%os.path.abspath(folder)); sys.exit(1)
    print("[BATCH] %d CSV file(s) in %s"%(len(csvs),os.path.abspath(folder)))
    rc=0
    for cp in csvs:
        stem=os.path.splitext(os.path.basename(cp))[0]
        od=os.path.join(args.output_dir, stem)
        cmd=[sys.executable, os.path.abspath(__file__), cp,
             "--outdir", od, "--output-dir", args.output_dir,
             "--endpoints", args.endpoints,
             "--server-min-flows", str(args.server_min_flows)]
        if args.config: cmd+=["--config", args.config]
        if args.no_flow: cmd.append("--no-flow")
        print("\n[BATCH] ==> %s  ->  %s"%(cp, od))
        r=subprocess.run(cmd); rc=rc or r.returncode
    print("\n[BATCH] done (%d file(s))."%len(csvs)); sys.exit(rc)

# ---- 単一CSV処理 (leaf) ----
CSV = single_file
DO_SRV = args.endpoints in ("servers","both")
DO_CLI = args.endpoints in ("clients","both")
MINF = args.server_min_flows
_stem = os.path.splitext(os.path.basename(CSV))[0]
OUTDIR = os.path.abspath(args.outdir) if args.outdir else os.path.abspath(os.path.join(args.output_dir, _stem))
os.makedirs(OUTDIR, exist_ok=True)
OUTFILE = os.path.join(OUTDIR, "gen_master_commands.txt")

# ---- 入力CSVの形式自動判定 + 正規化 ----
# 対応: (1) API形式(searchSubject.*/peer.* 機械名) はそのまま
#       (2) UI形式(Subject IP Address 等 人間可読、Total Bytes="56.49 M"、Duration="36min 38s"、
#           Peer Port/Protocol="2055/UDP") を API形式の必要列へ変換
def _parse_bytes(s):
    s=(s or "").strip()
    if not s or s=="--": return 0.0
    m=re.match(r"^([\d.]+)\s*([KMGTP]?)",s,re.I)
    if not m:
        try: return float(s)
        except: return 0.0
    mult={"":1,"K":1e3,"M":1e6,"G":1e9,"T":1e12,"P":1e15}.get(m.group(2).upper(),1)
    return float(m.group(1))*mult
def _parse_dur_ms(s):
    s=(s or "").strip().lower()
    if not s or s=="--": return 0
    tot=0.0
    for num,unit in re.findall(r"([\d.]+)\s*(d|h|hr|hours?|min|m|s|ms)",s):
        n=float(num)
        if unit=="d": tot+=n*86400
        elif unit in ("h","hr","hour","hours"): tot+=n*3600
        elif unit in ("min","m"): tot+=n*60
        elif unit=="ms": tot+=n/1000.0
        elif unit=="s": tot+=n
    return int(round(tot*1000))
def _parse_port(s):
    s=(s or "").strip()
    m=re.match(r"^(\d+)\s*/",s)
    if m: return int(m.group(1))
    return int(s) if s.isdigit() else -1
def normalize_csv(path):
    with open(path,newline="",encoding="utf-8",errors="replace") as fh:
        head=next(csv.reader(fh))
    if "searchSubject.ipAddress" in head: return path        # API形式: そのまま
    if "Subject IP Address" not in head:  return path        # 未知形式: 後段に委ねる
    ix={c:i for i,c in enumerate(head)}
    def g(v,c):
        j=ix.get(c); return v[j] if (j is not None and j<len(v)) else ""
    outp=os.path.join(OUTDIR,"_normalized_flow.csv")
    OUTCOLS=["searchSubject.ipAddress","peer.ipAddress","peer.portProtocol.port",
             "searchSubject.portProtocol.protocol","peer.synAckPackets",
             "connection.transferBytes","activeDuration"]
    with open(path,newline="",encoding="utf-8",errors="replace") as fh, \
         open(outp,"w",newline="",encoding="utf-8") as of:
        r=csv.reader(fh); next(r); w=csv.writer(of); w.writerow(OUTCOLS)
        for v in r:
            if not v or not g(v,"Subject IP Address"): continue
            try: sa=int(g(v,"Peer SYN/ACK Packets").strip())
            except: sa=0
            w.writerow([g(v,"Subject IP Address"), g(v,"Peer IP Address"),
                        _parse_port(g(v,"Peer Port/Protocol")),
                        (g(v,"protocol") or "").upper(), sa,
                        int(round(_parse_bytes(g(v,"Total Bytes")))),
                        _parse_dur_ms(g(v,"Duration"))])
    print("[INFO] UI-format CSV detected -> normalized:",outp)
    return outp
CSV = normalize_csv(CSV)

# ---- JSON config (optional overrides; falls back to code defaults) ----
# sna_to_ns_config.json は各キーが {"value": ..., "description": ...} 構造。
# cfg() は value を取り出す(旧来のフラット値や _description 等のメタキーも許容)。
cfgpath = os.path.abspath(args.config) if args.config else os.path.join(BASEDIR, "sna_to_ns_config.json")
CFG = {}
if os.path.exists(cfgpath):
    with open(cfgpath, encoding="utf-8") as f:
        CFG = json.load(f)
def cfg(key, default):
    v = CFG.get(key)
    if v is None: return default
    if isinstance(v, dict) and "value" in v: return v["value"]
    return v
SRV_MIN_BYTES = int(cfg("server_min_bytes", 5000))
REQ_SYNACK    = bool(cfg("require_tcp_synack", True))
INC_UDP       = bool(cfg("include_udp", True))
THRESH        = int(cfg("subnet_min_flows", THRESH))
# 手動オーバーライド(自動判定に追加され、競合時は手動が優先)
MAN_INSIDE = tuple(cfg("inside_public", list(INSIDE_PUBLIC)))
MAN_DC     = set(cfg("dc_force_regions", DC_FORCE_REGIONS))
MAN_SPUR   = set(cfg("spur_regions", SPUR_REGIONS))
NAME_MAP   = dict(cfg("name_map", NAME_MAP))
# 自動判定ノブ
AUTO_INSIDE     = bool(cfg("auto_inside_public", AUTO_INSIDE))
AUTO_DC         = bool(cfg("auto_dc_regions", AUTO_DC))
AUTO_SPUR       = bool(cfg("auto_spur_regions", AUTO_SPUR))
SPUR_LINK_RATIO = float(cfg("spur_link_ratio", SPUR_LINK_RATIO))

# ---- CIDR-based site definition (任意CIDRで拠点を明示。region自動集約より優先) ----
#   site_cidrs   : {"CIDR": "拠点名"}  例 {"10.10.0.0/16":"Tokyo","10.20.30.0/24":"SrvFarm"}
#   site_cidr_dc : 上記のうちDC(サーバ系/上段)として扱う拠点名のリスト
# 集約の最小単位は /24。/24より広いCIDRは内包する全/24を、/24より細かいCIDRは
# 重なる/24全体を、その拠点に割り当てる(最長プレフィックス一致が優先)。
SITE_CIDRS      = dict(cfg("site_cidrs", {}))
SITE_CIDR_DC    = set(cfg("site_cidr_dc", []))
CIDR_SITE_NAMES = set(SITE_CIDRS.values())
_site_nets=[]
for _cidr,_nm in SITE_CIDRS.items():
    try: _site_nets.append((ipaddress.ip_network(_cidr,strict=False),_nm))
    except ValueError: print("[WARN] invalid CIDR in site_cidrs: %r"%_cidr)
_site_nets.sort(key=lambda x:-x[0].prefixlen)   # 最長プレフィックス優先
def cidr_site_ip(ip):
    try: a=ipaddress.ip_address(ip)
    except ValueError: return None
    for net,nm in _site_nets:
        if a in net: return nm
    return None
def cidr_site_sub(k24str):
    # /24キー("a.b.c") に重なる最長一致CIDRの拠点名 (無ければ None)
    if not k24str: return None
    try: s24=ipaddress.ip_network(k24str+".0/24",strict=False)
    except ValueError: return None
    for net,nm in _site_nets:
        if net.overlaps(s24): return nm
    return None

def dq(o): return json.dumps(o).replace('"',"'")
def wrap(lst,n): return [lst[i:i+n] for i in range(0,len(lst),n)]
def is_rfc1918(ip):
    if ip.startswith("10.") or ip.startswith("192.168."): return True
    if ip.startswith("172."):
        try: return 16<=int(ip.split(".")[1])<=31
        except: return False
    return False
def _oct(ip,i):
    try: return int(ip.split(".")[i])
    except: return -1
def is_special(ip):
    # 自組織公開帯になり得ない予約/特殊用途レンジ (RFC5735/6598/multicast等)
    a=_oct(ip,0); b=_oct(ip,1)
    if a in (0,127): return True
    if a>=224: return True                               # multicast/予約 224-255
    if ip.startswith("169.254."): return True            # link-local
    if a==100 and 64<=b<=127: return True                # CGNAT 100.64/10
    if a==198 and b in (18,19): return True              # benchmark 198.18/15
    if ip.startswith(("192.0.2.","198.51.100.","203.0.113.")): return True  # doc
    return False
def is_inside(ip):
    return is_rfc1918(ip) or any(ip.startswith(p) for p in INSIDE_PUBLIC)
def k24(ip):
    p=ip.split("."); return (p[0]+"."+p[1]+"."+p[2]) if len(p)==4 else None
def reg16(ip):
    p=ip.split("."); return (p[0]+"."+p[1]) if len(p)==4 else None
def last_oct(ip):
    try: return int(ip.split(".")[3])
    except: return -1

# ---------- AUTO: 自組織公開帯(inside_public)の推定 (本処理前のプリスキャン) ----------
# 自組織の公開/16は「発信(outinit)が支配的」= 社内ユーザ/プロキシが外部へ大量に発信する。
#   outinit = searchSubject(=発信側) が公開ピア宛に出したフロー数。
#   インターネット上のサーバは subject に現れない(outinit≈0)。
# 判定: special-use帯を除外し、outinit >= 絶対閾値 かつ outinit/総数 >= 比率 の/16のみ採用。
# (散在する外部クライアントや人気サービスは比率/絶対値で除外される)
INSIDE_OUTINIT_MIN   = int(cfg("inside_outinit_min", 2000))
INSIDE_OUTINIT_RATIO = float(cfg("inside_outinit_ratio", 0.6))
auto_inside=set()
if AUTO_INSIDE:
    pub_oi=collections.Counter(); pub_tot=collections.Counter()
    with open(CSV,newline="",encoding="utf-8",errors="replace") as fh:
        r=csv.reader(fh); cols=next(r); ix={c:i for i,c in enumerate(cols)}
        Sip=ix["searchSubject.ipAddress"];Pip=ix["peer.ipAddress"]
        mx=max(Sip,Pip)
        for v in r:
            if len(v)<=mx: continue
            sip=v[Sip];pip=v[Pip]
            rs=is_rfc1918(sip); rp=is_rfc1918(pip)
            if not rs and not is_special(sip):
                rg=reg16(sip); pub_tot[rg]+=1
                if not rp: pub_oi[rg]+=1            # 公開subject -> 公開peer = 社内発信
            if not rp and not is_special(pip):
                pub_tot[reg16(pip)]+=1
    for rg,oi in pub_oi.items():
        if oi>=INSIDE_OUTINIT_MIN and oi>=INSIDE_OUTINIT_RATIO*pub_tot[rg]:
            auto_inside.add(rg+".")
# 手動を統合(競合は手動優先=和集合に手動を必ず含める)
INSIDE_PUBLIC = tuple(sorted(set(auto_inside) | set(MAN_INSIDE)))

# 外部(インターネット)サービスの実サービスポート判定(従来どおり: 内部サーバ検出には未使用)
KNOWN_HIGH={1433,1521,3306,3389,5432,5060,5061,8080,8443,8000,5989,5985,5986,
            1645,1812,1813,9100,52311,7778,10000,3268,3269,2049}
def is_service(p): return p>0 and (p<1024 or p in KNOWN_HIGH)

# ---------- per /24 features + endpoint detection ----------
class Sub:
    __slots__=("flows","bytes","octs","srv","cli","reg")
    def __init__(s,reg): s.flows=0;s.bytes=0.0;s.octs=set();s.srv=0;s.cli=0;s.reg=reg
sub={}
mat=collections.Counter()        # (regA,regB) inter-region flows
regflows=collections.Counter()   # region total internal flows
ports_bytes=collections.defaultdict(collections.Counter)  # inside server IP -> {port: transferBytes}
ports_flows=collections.defaultdict(collections.Counter)  # inside server IP -> {port: flows}
srv_ip_clients=collections.defaultdict(set)               # inside server IP -> {client ip,...}
svc_bytes=collections.Counter()      # (proto,port) external service -> bytes
svc_flows=collections.Counter()      # (proto,port) external service -> flows
svc_ips=collections.defaultdict(set) # (proto,port) external service -> {server IP,...}
seg_cli_hosts=collections.defaultdict(set)   # /24セグメント -> {クライアント(発信)側ホストIP,...}

with open(CSV,newline="",encoding="utf-8",errors="replace") as fh:
    r=csv.reader(fh); cols=next(r); ix={c:i for i,c in enumerate(cols)}
    # orientation 固定: searchSubject = client, peer = server
    Sip=ix["searchSubject.ipAddress"];Pip=ix["peer.ipAddress"]
    Pp=ix["peer.portProtocol.port"]                      # サーバ(=サービス)ポート
    PrS=ix["searchSubject.portProtocol.protocol"]        # フローのL4プロトコル
    pSA=ix["peer.synAckPackets"]                          # サーバが接続を受理した証拠
    By=ix["connection.transferBytes"]
    maxix=max(Sip,Pip,Pp,PrS,pSA,By)
    for v in r:
        if len(v)<=maxix: continue
        sip=v[Sip];pip=v[Pip]                            # sip=client, pip=server
        proto=(v[PrS] or "").upper()
        try: sport=int(v[Pp])
        except: sport=-1
        try: by=float(v[By])
        except: by=0.0
        try: psa=int(v[pSA])
        except: psa=0
        ina,inb=is_inside(sip),is_inside(pip)
        # inter-region matrix (internal-internal)
        if ina and inb:
            ra,rb=reg16(sip),reg16(pip)
            if ra and rb:
                regflows[ra]+=1; regflows[rb]+=1
                if ra!=rb: mat[tuple(sorted((ra,rb)))]+=1
        # server-flow acceptance (orientation + handshake/proto)
        if proto=="TCP":
            ok = (psa>0) if REQ_SYNACK else True
        elif proto=="UDP":
            ok = INC_UDP
        else:
            ok = False                                   # ICMP 等はサービス対象外
        # endpoint detection (server = peer side)
        if ok and sport>0:
            if inb:                                      # 内部サーバ
                ports_bytes[pip][sport]+=by
                ports_flows[pip][sport]+=1
                srv_ip_clients[pip].add(sip)
            elif ina and is_service(sport):              # 外部(インターネット)サービス(実サービスポートのみ)
                svc_bytes[(proto,sport)]+=by
                svc_flows[(proto,sport)]+=1
                svc_ips[(proto,sport)].add(pip)          # 該当サービスの外部サーバIPを記録
        # /24 aggregation + role (sip=client, pip=server)
        if ina:
            kk=k24(sip); e=sub.get(kk)
            if e is None: e=Sub(reg16(sip)); sub[kk]=e
            e.flows+=1; e.bytes+=by; e.cli+=1
            seg_cli_hosts[kk].add(sip)           # セグメント別 クライアント(発信)ホスト数の母集合
            try: e.octs.add(int(sip.split(".")[3]))
            except: pass
        if inb:
            kk=k24(pip); e=sub.get(kk)
            if e is None: e=Sub(reg16(pip)); sub[kk]=e
            e.flows+=1; e.bytes+=by
            if ok: e.srv+=1
            try: e.octs.add(int(pip.split(".")[3]))
            except: pass

adopted={k:e for k,e in sub.items() if e.flows>=THRESH}

# ---------- region features ----------
reg_members=collections.defaultdict(list)
for k,e in adopted.items(): reg_members[e.reg].append(k)
def is_userlan(e):
    hosts=len(e.octs); gw=1 in e.octs
    return hosts>=20 and e.cli>e.srv and gw
reg_user=collections.Counter(); reg_srv=collections.Counter(); reg_cli=collections.Counter()
for reg,ks in reg_members.items():
    for k in ks:
        e=adopted[k]
        reg_srv[reg]+=e.srv; reg_cli[reg]+=e.cli
        if is_userlan(e): reg_user[reg]+=1

regions=set(reg_members)
# --- AUTO: DC拠点判定 (サーバ系セグメント数 > ユーザ系セグメント数) + 手動強制 ---
def reg_is_dc_auto(reg):
    ks=reg_members[reg]
    n_srv_seg=sum(1 for k in ks if adopted[k].srv>adopted[k].cli)
    n_user_seg=len(ks)-n_srv_seg  # cli >= srv のセグメント数
    return n_srv_seg>n_user_seg   # 同数の場合は DC としない
auto_dc=set(r for r in regions if AUTO_DC and reg_is_dc_auto(r))
dc_regs=auto_dc | set(r for r in regions if r in MAN_DC)   # 手動優先(必ず含む)
non_dc=[r for r in regions if r not in dc_regs]

# シード(主要ハブ) = DC以外で内部flows最大の上位K
seeds=sorted(non_dc,key=lambda r:-regflows.get(r,0))[:K_CAMPUS]
def best_link(reg):
    return max((mat.get(tuple(sorted((reg,s))),0) for s in seeds if s!=reg), default=0)
# --- AUTO: スパー(孤立)拠点判定: どのシードとも結合が弱い + 手動強制 ---
auto_spur=set()
if AUTO_SPUR:
    for r in non_dc:
        if r in seeds: continue
        rf=regflows.get(r,0)
        if rf>0 and best_link(r) < SPUR_LINK_RATIO*rf:
            auto_spur.add(r)
spur_regs=(auto_spur | set(r for r in regions if r in MAN_SPUR)) - set(seeds) - dc_regs
campus_regs=[r for r in non_dc if r not in spur_regs]

def strongest_seed(reg):
    best=None;bw=-1
    for s in seeds:
        w=mat.get(tuple(sorted((reg,s))),0)
        if reg==s: w=10**9
        if w>bw: bw=w;best=s
    return best if best is not None else reg

region_site={}
for reg in campus_regs:
    region_site[reg]=NAME_MAP.get(strongest_seed(reg), "Campus-%s"%strongest_seed(reg))
for reg in dc_regs:
    region_site[reg]=DC_SITE_NAME if MERGE_ALL_DC else ("DC-%s"%reg)
for reg in spur_regs:
    region_site[reg]=SPUR_NAME.get(reg,"Site-%s"%reg.replace(".","-"))

# サブネット(/24) -> 拠点。CIDR明示(site_cidrs)があれば region 自動集約より優先。
site_subnets=collections.defaultdict(list)
site_of_sub={}
for k,e in adopted.items():
    s=cidr_site_sub(k)
    if s is None: s=region_site[e.reg]
    site_of_sub[k]=s; site_subnets[s].append((k,e))
# 拠点 -> 所属/16 (サマリ表示用に site_subnets から再構築)
site_regs=collections.defaultdict(list)
for s,subs in site_subnets.items():
    site_regs[s]=sorted(set(e.reg for k,e in subs))

def site_is_dc(site):
    if site in SITE_CIDR_DC: return True               # CIDR拠点のDC明示指定
    subs=site_subnets[site]
    regs=set(e.reg for k,e in subs)
    if regs and regs<=dc_regs and site not in CIDR_SITE_NAMES: return True
    n_srv_seg=sum(1 for k,e in subs if e.srv>e.cli)
    n_user_seg=sum(1 for k,e in subs if e.cli>=e.srv)
    return n_srv_seg>n_user_seg    # 同数の場合は DC としない
dc_sites=sorted([s for s in site_subnets if site_is_dc(s)])
client_sites=[s for s in site_subnets if not site_is_dc(s)]
def site_flow(s): return sum(e.flows for k,e in site_subnets[s])
client_sites=sorted(client_sites,key=lambda s:-site_flow(s))

# Fallback 1: DCなし → サーバ系フロー数が最大の拠点をDCとする
if not dc_sites and client_sites:
    def _site_srv_flows(s):
        return sum(e.srv for k,e in site_subnets[s])
    _best=max(client_sites,key=_site_srv_flows)
    if _site_srv_flows(_best)>0:
        dc_sites=[_best]
        client_sites=[s for s in client_sites if s!=_best]

# Fallback 2: サーバが全くない → クライアントIP数が最大の拠点をDCとする
if not dc_sites and client_sites:
    def _site_cli_ips(s):
        return sum(len(seg_cli_hosts.get(k,())) for k,e in site_subnets[s])
    _best=max(client_sites,key=_site_cli_ips)
    dc_sites=[_best]
    client_sites=[s for s in client_sites if s!=_best]

client_set=set(client_sites)
site_order=dc_sites+client_sites

# 拠点ごとに一意な短縮コード(インフラ機器名用)。自動命名で衝突しないようサフィックス付与。
def _basecode(site):
    c=CODE_MAP.get(site)
    if c: return c
    return ("".join(ch for ch in site if ch.isalnum()).upper()[:6]) or "SITE"
_code_used=collections.Counter(); _code_of={}
for _s in site_order:
    _b=_basecode(_s)
    _code_of[_s]=_b if _code_used[_b]==0 else "%s%d"%(_b,_code_used[_b])
    _code_used[_b]+=1
def code(site): return _code_of[site]

# 4-char unique area code for endpoint naming
def build_acode(names):
    used=collections.Counter(); res={}
    for nm in names:
        base="".join(ch for ch in nm if ch.isalnum())[:4] or "Area"
        if used[base]==0: res[nm]=base
        else: res[nm]=base+str(used[base])
        used[base]+=1
    return res
acode=build_acode(site_order)

# ---------- VLAN assignment (deterministic, per site by flows desc) ----------
seg_vlan={}; site_svis={}
vlan=101
for s in site_order:
    out=[]
    for k,e in sorted(site_subnets[s],key=lambda x:-x[1].flows):
        seg_vlan[k]=vlan; out.append((k,e,vlan)); vlan+=1
    site_svis[s]=out

# ---------- endpoint sets ----------
def site_access(site):
    c=code(site)
    return (c+"-Acc1") if site in client_set else (c+"-Core")

servers=[]   # (name, ip, vlanname, site)
srv_meta={}  # ip -> (max_port_bytes, total_bytes, top_port, distinct_clients)  (out-of-scope記録用)
oos=[]       # out-of-scope: (ip, region, reason, max_port_bytes, total_bytes, top_port, distinct_clients)
n_cand=0     # orientation 基準のサーバ候補IP数
if DO_SRV:
    per=collections.Counter()   # (site, port-label) -> 連番
    cand=sorted(ports_bytes.keys(),
                key=lambda ip:(-sum(ports_bytes[ip].values()), ip))
    for ip in cand:
        n_cand+=1
        pb=ports_bytes[ip]
        tot=int(sum(pb.values())); mx=int(max(pb.values()) if pb else 0)
        topp=max(pb.items(),key=lambda x:x[1])[0] if pb else -1
        ncl=len(srv_ip_clients.get(ip,()))
        # bytes 閾値を超えた実サービスポートのみ採用 (+ 任意のフロー下限 MINF)
        qports=sorted(p for p,b in pb.items()
                      if b>=SRV_MIN_BYTES and ports_flows[ip][p]>=MINF)
        if not qports:
            oos.append((ip,reg16(ip),"below_traffic_threshold",mx,tot,topp,ncl)); continue
        seg=k24(ip)
        if seg not in adopted or last_oct(ip)==1:
            oos.append((ip,reg16(ip),"segment_not_adopted_or_gateway",mx,tot,topp,ncl)); continue
        site=site_of_sub.get(seg) or region_site.get(reg16(ip))
        plabel="-".join(str(p) for p in qports)
        per[(site,plabel)]+=1
        servers.append(("SRV_%s_%s_%d"%(acode[site],plabel,per[(site,plabel)]),
                        ip, "Vlan%d"%seg_vlan[seg], site))
        srv_meta[ip]=(mx,tot,topp,ncl)

# ---------- segment classification (server-segment vs client-segment) ----------
# 同一/24セグメント内にサーバとクライアントが混在する場合は「台数の多い方」へ帰属させる。
#   server-segment : 採用サーバ台数 > クライアント専用ホスト台数
#   client-segment : それ以外(クライアント>=サーバ、またはサーバ0)
# server-segment はサーバ群を生成し(後段で FW 配下に分離)、client-segment は PC を1台生成する。
seg_srv_hosts=collections.defaultdict(set)
for nm,ip,vn,st in servers: seg_srv_hosts[k24(ip)].add(ip)
def _seg_is_server(seg):
    ns=len(seg_srv_hosts.get(seg,()))
    nc=len(seg_cli_hosts.get(seg,set())-seg_srv_hosts.get(seg,set()))
    return ns>0 and ns>nc
server_seg=set(k for k in adopted if _seg_is_server(k))
# client-majority セグメントに居たサーバ候補は out-of-scope(分離対象外)へ移す
kept=[]
for nm,ip,vn,st in servers:
    if k24(ip) in server_seg: kept.append((nm,ip,vn,st))
    else:
        mx,tot,topp,ncl=srv_meta.get(ip,(0,0,-1,0))
        oos.append((ip,reg16(ip),"client_majority_segment",mx,tot,topp,ncl))
servers=kept

pcs=[]       # (name, vlanname, site)
pc_name_by_seg={}   # /24セグメント -> PCデバイス名 (フローCSVのIP->名称解決用)
if DO_CLI:
    per=collections.Counter()
    for s in site_order:
        for k,e,vl in site_svis[s]:
            if k in server_seg: continue          # server-segment は PC を作らない
            if e.cli<=0: continue                 # クライアント実績の無いセグメントは除外
            per[s]+=1
            n_cli_ips=len(seg_cli_hosts.get(k,()))
            nm="PC_%s_%d_%d"%(acode[s],n_cli_ips,per[s])
            pcs.append((nm, "Vlan%d"%vl, s)); pc_name_by_seg[k]=nm

svcs=[]      # (name, proto, port, flows)
svc_oos=0    # 閾値未満で除外した外部サービス数
# Internet WayPoint 共有セグメント用に次のVLAN番号を予約
inet_vlan=vlan; vlan+=1   # vlan は全内部サブネット割当後の次空き番号
INET_VLAN_NAME="VlanIntSvc"
if DO_SRV:
    for (proto,port),b in sorted(svc_bytes.items(),key=lambda x:-x[1]):
        fl=svc_flows[(proto,port)]
        if b<SRV_MIN_BYTES or fl<MINF:
            svc_oos+=1; continue
        n_ips=len(svc_ips[(proto,port)])
        svcs.append(("Svc_%s%d_%d"%(proto,port,n_ips), proto, port, fl))

# ---------- server/PC separation helpers (FW between server & client segments) ----------
srv_sites=set(st for nm,ip,vn,st in servers)
pc_sites =set(st for nm,vn,st in pcs)
def site_mixed(s):
    # 同一エリアにサーバセグメントとクライアントセグメントが共存する client 拠点
    return (s in srv_sites) and (s in pc_sites) and (s in client_set)
def srv_access(site):
    return (code(site)+"-SrvSw") if site_mixed(site) else site_access(site)
def cli_access(site):
    return site_access(site)

def mixed_client_grid(site):
    # mixed client 拠点の device_location。境界FW配下に SrvSw を新設しサーバを分離配置:
    #   左列(col0)縦 : Edge - FW - Core - Acc1 - (PCを下方向へ EP_ROW_WIDTH 折り返し)
    #   右側列        : SrvSw(Core と同じ行) - (サーバを下方向へ1列で縦積み)
    # PC帯(左)とサーバ帯(右)は空き列で分離し、L1リンクの交差を避ける。
    c=code(site)
    pcs_s=[nm for nm,vn,st in pcs if st==site]
    srv_s=[nm for nm,ip,vn,st in servers if st==site]
    Wpc=max(1,min(len(pcs_s),EP_ROW_WIDTH))
    Wsrv=max(1,min(len(srv_s),EP_ROW_WIDTH))
    col_srv=Wpc+1                          # サーバ帯の開始列(PC帯との間に空き列を1つ挟む)
    g={}
    g[(0,0)]="%s-Edge"%c
    g[(1,0)]="%s-FW"%c
    g[(2,0)]="%s-Core"%c
    g[(2,col_srv)]="%s-SrvSw"%c
    g[(3,0)]="%s-Acc1"%c
    for i,nm in enumerate(pcs_s):          # PC: Acc1 直下(r4-)に左詰めで折り返し
        g[(4+i//Wpc, i%Wpc)]=nm
    for i,nm in enumerate(srv_s):          # サーバ: SrvSw 直下(r3-)にサーバ帯内で折り返し
        g[(3+i//Wsrv, col_srv+(i%Wsrv))]=nm
    maxr=max(r for r,_ in g); maxc=max(cc for _,cc in g)
    return [[g.get((r,cc),"_AIR_") for cc in range(maxc+1)] for r in range(maxr+1)]

# ---------- build commands ----------
cmds=[]
# 1) area_location
grid=[]
if DO_SRV and svcs: grid.append(["Internet-Svc"])
grid.append(["Internet_wp_"])
if dc_sites: grid.append(list(dc_sites))
grid.append(["WAN_wp_"])
grid.append(list(client_sites))
cmds.append('add area_location "%s"'%dq(grid))

# 2) device_location (infra + endpoints at bottom)
def eps_of(site):
    return [nm for nm,ip,vn,st in servers if st==site]+[nm for nm,vn,st in pcs if st==site]
for s in dc_sites:
    c=code(s); rows=[["%s-FW"%c],["%s-Core"%c]]+wrap(eps_of(s),EP_ROW_WIDTH)
    cmds.append('add device_location "%s"'%dq([s,rows]))
for s in client_sites:
    if site_mixed(s):
        cmds.append('add device_location "%s"'%dq([s,mixed_client_grid(s)]))
    else:
        c=code(s); rows=[["%s-Edge"%c],["%s-FW"%c],["%s-Core"%c],["%s-Acc1"%c]]+wrap(eps_of(s),EP_ROW_WIDTH)
        cmds.append('add device_location "%s"'%dq([s,rows]))
# Internet-Svc: Internet(waypoint)はエリアの直下にあるため、全サーバを単一の最下段行に
# 並べて下方向へ接続させる(複数行だと上段サーバの線が下段を跨いで交差するのを防ぐ)。
if DO_SRV and svcs:
    cmds.append('add device_location "%s"'%dq(["Internet-Svc",[[nm for nm,_,_,_ in svcs]]]))

# 3) L1 links (infra + endpoints + svc)
links=[]; wan_p=0; inet_p=0
for s in dc_sites:
    c=code(s)
    links.append(["%s-Core"%c,"%s-FW"%c,"GigabitEthernet 0/1","GigabitEthernet 0/1"])
    links.append(["%s-FW"%c,"Internet","GigabitEthernet 0/2","port %d"%inet_p]); inet_p+=1
    links.append(["%s-Core"%c,"WAN","GigabitEthernet 0/2","port %d"%wan_p]); wan_p+=1
for s in client_sites:
    c=code(s)
    links.append(["%s-Edge"%c,"%s-FW"%c,"GigabitEthernet 0/1","GigabitEthernet 0/1"])
    links.append(["%s-FW"%c,"%s-Core"%c,"GigabitEthernet 0/2","GigabitEthernet 0/2"])
    links.append(["%s-Core"%c,"%s-Acc1"%c,"GigabitEthernet 0/1","GigabitEthernet 0/1"])
    links.append(["%s-Edge"%c,"WAN","GigabitEthernet 0/2","port %d"%wan_p]); wan_p+=1
    if site_mixed(s):                      # サーバ用SW を境界FW配下に接続(サーバ⇔PC間はFW経由)
        links.append(["%s-FW"%c,"%s-SrvSw"%c,"GigabitEthernet 0/3","GigabitEthernet 0/1"])
# endpoints: connect to access switch; remember switch-port for L2 access binding
#   サーバ -> srv_access (mixed拠点は SrvSw、それ以外は従来のアクセス) / PC -> cli_access
sw_n=collections.Counter(); ep_access=[]   # (sw, swport, vlanname)
for nm,ip,vn,st in servers:
    sw=srv_access(st); sw_n[sw]+=1; swp="GigabitEthernet 1/0/%d"%sw_n[sw]
    links.append([nm,sw,"GigabitEthernet 0/0",swp]); ep_access.append((sw,swp,vn))
for nm,vn,st in pcs:
    sw=cli_access(st); sw_n[sw]+=1; swp="GigabitEthernet 1/0/%d"%sw_n[sw]
    links.append([nm,sw,"GigabitEthernet 0/0",swp]); ep_access.append((sw,swp,vn))
inet_p_svc_start=inet_p   # Svc デバイスが使用する Internet 側ポートの開始番号を記録
for nm,proto,port,fl in svcs:
    links.append([nm,"Internet","GigabitEthernet 0/0","port %d"%inet_p]); inet_p+=1
for batch in wrap(links,CHUNK):
    cmds.append('add l1_link_bulk "%s"'%dq(batch))

# 4) port_info (all devices 1Gbps; waypoints N/A)
alldev=[]
for s in dc_sites: c=code(s); alldev+=["%s-Core"%c,"%s-FW"%c]
for s in client_sites:
    c=code(s); alldev+=["%s-Edge"%c,"%s-FW"%c,"%s-Core"%c,"%s-Acc1"%c]
    if site_mixed(s): alldev.append("%s-SrvSw"%c)
alldev+=[nm for nm,_,_,_ in servers]+[nm for nm,_,_ in pcs]+[nm for nm,_,_,_ in svcs]
for batch in wrap(alldev,CHUNK):
    cmds.append('rename port_info_bulk "%s"'%dq([[batch,"_ALL_",["1Gbps","Full","1000BASE-T"]]]))
cmds.append('rename port_info_bulk "%s"'%dq([[["WAN","Internet"],"_ALL_",["N/A","N/A","N/A"]]]))

# --- Attribute sheet: color constants & helpers ---
# Palette shared with cv_converter / cml_converter (observed=green / inferred=gray).
C_INFRA  = [200, 200, 200]  # light gray   — inferred/synthesised devices + inferred WayPoints
C_SERVER = [255, 204, 204]  # light red    — server-role endpoints (inside + internet)
C_PC     = [255, 255, 204]  # very light yellow — client endpoints
C_NET    = [235, 241, 222]  # light green  — OBSERVED (real) network gear
C_WP_OBS = [220, 230, 242]  # light blue   — observed network-device WayPoint (reserved; not emitted today)

# Network Sketcher stencils that denote network gear (vs server/client endpoints).
_NET_STENCILS = {"Router", "L3Switch", "Switch", "Firewall", "WLC", "AP"}

def _role_color(stencil, observed=False):
    """Default-cell colour by role, unified across the sna / cv / cml converters.

    observed=green / inferred=gray. SNA synthesises all of its network gear from
    the flow data, so network stencils pass observed=False and render gray; the
    green branch is reserved for any network device the flow data surfaces as
    real (kept consistent with cv_/cml_converter).
    """
    if stencil == "Server":
        return C_SERVER
    if stencil == "PC":
        return C_PC
    if stencil in _NET_STENCILS:
        return C_NET if observed else C_INFRA
    return C_INFRA  # Cloud / WayPoint / unknown

def _strip_nums(m):
    """Remove tokens containing digits: 'Catalyst 9300' → 'Catalyst'."""
    return ' '.join(w for w in m.split() if not any(c.isdigit() for c in w))

def _ac(text, rgb):
    """Return escaped attribute cell string: \\\"['text',[R,G,B]]\\\"."""
    return "\\\"['%s',[%d,%d,%d]]\\\"" % (text, rgb[0], rgb[1], rgb[2])

def _attr_row_str(row):
    """Format one attr_row as the inner list string for rename_attribute_bulk."""
    name, defv, model, os_val, stencil = row
    parts = ["'%s'" % name]
    if isinstance(defv, (list, tuple)):   # colored cell
        parts.append(_ac(defv[0], defv[1]))
    else:                                  # plain string (WayPoint / header)
        parts.append("'%s'" % defv)
    parts.append("'%s'" % _strip_nums(model))
    parts.append("'%s'" % os_val)
    parts.append("'%s'" % stencil)
    return '[%s]' % ','.join(parts)

# 5) SVI/L2/IP per site (SVIs on site Core)
attr_rows=[["Device Name","Default","Model","OS","Stencil Type"]]
for s in dc_sites:
    c=code(s); core="%s-Core"%c
    svis=[]; binds=[]; ips=[]
    for k,e,vl in site_svis[s]:
        svis.append("Vlan %d"%vl); binds.append([core,"Vlan %d"%vl,["Vlan%d"%vl]])
        ips.append([core,"Vlan %d"%vl,[k+".1/24"]])
    cmds.append('add virtual_port_bulk "%s"'%dq([[core,svis]]))
    cmds.append('add l2_segment_bulk "%s"'%dq(binds))
    cmds.append('add ip_address_bulk "%s"'%dq(ips))
    attr_rows.append([core,("DEVICE",_role_color("L3Switch")),"Nexus 9336C","NX-OS","L3Switch"])
    attr_rows.append(["%s-FW"%c,("DEVICE",_role_color("Firewall")),"Secure Firewall 4115","FTD","Firewall"])
for s in client_sites:
    c=code(s); core="%s-Core"%c; acc="%s-Acc1"%c; fw="%s-FW"%c; ssw="%s-SrvSw"%c
    mx=site_mixed(s)
    # mixed拠点ではサーバセグメントのSVIをFWへ(=サーバ⇔クライアント間はFWで分離)。
    cli_segs=[(k,e,vl) for (k,e,vl) in site_svis[s] if not (mx and k in server_seg)]
    srv_segs=[(k,e,vl) for (k,e,vl) in site_svis[s] if (mx and k in server_seg)]
    # client segments: SVI on Core, trunk Core<->Acc1
    csvis=[]; cbinds=[]; cips=[]; cvn=[]
    for k,e,vl in cli_segs:
        csvis.append("Vlan %d"%vl); cbinds.append([core,"Vlan %d"%vl,["Vlan%d"%vl]])
        cvn.append("Vlan%d"%vl); cips.append([core,"Vlan %d"%vl,[k+".1/24"]])
    if csvis:
        cmds.append('add virtual_port_bulk "%s"'%dq([[core,csvis]]))
        cmds.append('add l2_segment_bulk "%s"'%dq(cbinds))
        cmds.append('add l2_segment_bulk "%s"'%dq([[core,"GigabitEthernet 0/1",cvn],
                                                   [acc,"GigabitEthernet 0/1",cvn]]))
        cmds.append('add ip_address_bulk "%s"'%dq(cips))
    # server segments (mixed only): SVI on FW, trunk FW<->SrvSw
    if mx and srv_segs:
        ssvis=[]; sbinds=[]; sips=[]; svn=[]
        for k,e,vl in srv_segs:
            ssvis.append("Vlan %d"%vl); sbinds.append([fw,"Vlan %d"%vl,["Vlan%d"%vl]])
            svn.append("Vlan%d"%vl); sips.append([fw,"Vlan %d"%vl,[k+".1/24"]])
        cmds.append('add virtual_port_bulk "%s"'%dq([[fw,ssvis]]))
        cmds.append('add l2_segment_bulk "%s"'%dq(sbinds))
        cmds.append('add l2_segment_bulk "%s"'%dq([[fw,"GigabitEthernet 0/3",svn],
                                                   [ssw,"GigabitEthernet 0/1",svn]]))
        cmds.append('add ip_address_bulk "%s"'%dq(sips))
    attr_rows.append(["%s-Edge"%c,("DEVICE",_role_color("Router")),"Catalyst 8300","IOS-XE","Router"])
    attr_rows.append([fw,("DEVICE",_role_color("Firewall")),"Secure Firewall 3120","FTD","Firewall"])
    attr_rows.append([core,("DEVICE",_role_color("L3Switch")),"Catalyst 9500","IOS-XE","L3Switch"])
    attr_rows.append([acc,("DEVICE",_role_color("Switch")),"Catalyst 9300","IOS-XE","Switch"])
    if mx: attr_rows.append([ssw,("DEVICE",_role_color("Switch")),"Catalyst 9300","IOS-XE","Switch"])

# 6) endpoint access L2 (switch side, 1 VLAN) + server physical-port IP (RULE 11.5)
for batch in wrap([[sw,swp,[vn]] for (sw,swp,vn) in ep_access],CHUNK):
    cmds.append('add l2_segment_bulk "%s"'%dq(batch))
for batch in wrap([[nm,"GigabitEthernet 0/0",[ip+"/24"]] for nm,ip,vn,st in servers],CHUNK):
    cmds.append('add ip_address_bulk "%s"'%dq(batch))

# 6b) Internet WayPoint 共有セグメント
# 構成: Internet WayPoint に SVI(Vlan N)を作成し、SVI 自体と
#       各 Svc デバイスに接続している Internet 側の物理ポート (port 0, port 1, …) を
#       同一 L2 セグメント (VlanIntSvc) にバインドする。
# Svc デバイス側の GE0/0 は L3 インタフェースのまま変更しない。
if DO_SRV and svcs:
    inet_svi="Vlan %d"%inet_vlan
    # Internet WayPoint に SVI を追加
    cmds.append('add virtual_port_bulk "%s"'%dq([["Internet",[inet_svi]]]))
    # SVI 本体を L2 セグメントにバインド (RULE: SVI は必ず l2_segment_bulk でバインド)
    cmds.append('add l2_segment_bulk "%s"'%dq(
        [["Internet",inet_svi,[INET_VLAN_NAME]]]))
    # Internet 側の各物理ポート (port N) を同一 L2 セグメントにバインド
    inet_ports=[["Internet","port %d"%(inet_p_svc_start+i),[INET_VLAN_NAME]]
                for i in range(len(svcs))]
    for batch in wrap(inet_ports,CHUNK):
        cmds.append('add l2_segment_bulk "%s"'%dq(batch))

# 7) attributes (endpoints + waypoints)
for nm,ip,vn,st in servers: attr_rows.append([nm,("DEVICE",_role_color("Server")),"UCS C220 M6","Linux","Server"])
for nm,vn,st in pcs:        attr_rows.append([nm,("DEVICE",_role_color("PC")),"Workstation","Windows","PC"])
for nm,proto,port,fl in svcs: attr_rows.append([nm,("DEVICE",_role_color("Server")),"Internet Service","-","Server"])
attr_rows.append(["WAN",("WayPoint",_role_color("Cloud")),"","","Cloud"])
attr_rows.append(["Internet",("WayPoint",_role_color("Cloud")),"","","Cloud"])
_attr_hdr="['Device Name','Default','Model','OS','Stencil Type']"
for batch in wrap(attr_rows[1:],CHUNK):
    rows_str=','.join(_attr_row_str(r) for r in batch)
    cmds.append('rename attribute_bulk "[%s,%s]"'%(_attr_hdr,rows_str))

# ---------- write & summary ----------
with open(OUTFILE,"w",encoding="utf-8") as f: f.write("\n".join(cmds))

# out-of-scope IP CSV (candidate servers that were not adopted)
OOSFILE=os.path.join(OUTDIR,"out_of_scope_ips.csv")
reason_cnt=collections.Counter()
if DO_SRV:
    with open(OOSFILE,"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        w.writerow(["ip","region","reason","max_port_bytes","total_bytes","top_port","distinct_clients"])
        for rec in sorted(oos,key=lambda x:(x[2],-x[4])):
            w.writerow(rec); reason_cnt[rec[2]]+=1

# ---------- [FLOW] paste CSV (gen_flow_list.csv) : [FLOW]test2.xlsx 貼り付け用 ----------
# 各行 = (Source/Dest デバイス名, proto, サービスポート) 単位。
# Max.bandwidth(Mbps) = connection.transferBytes(受信+送信合計)*8 / activeDuration(秒)
#   同一種別フローが複数あれば最大Mbpsを採用。Manual/Automatic routing列は対象外(空欄)。
# デバイス名はマスタ定義名(SRV_*/PC_*/Svc_*)で出力。両端が解決できないフローは除外。
FLOWFILE=os.path.join(OUTDIR,"gen_flow_list.csv")
n_flow=0
if not args.no_flow:
    SERVNAME={20:"FTP-Data",21:"FTP",22:"SSH",23:"Telnet",25:"SMTP",53:"DNS",
        67:"DHCP",68:"DHCP",69:"TFTP",80:"HTTP",88:"Kerberos",110:"POP3",111:"RPC",
        123:"NTP",135:"MSRPC",137:"NetBIOS-NS",138:"NetBIOS-DGM",139:"NetBIOS-SSN",
        143:"IMAP",161:"SNMP",162:"SNMPTRAP",179:"BGP",389:"LDAP",443:"HTTPS",445:"SMB",
        465:"SMTPS",514:"Syslog",515:"LPD",587:"SMTP-Sub",636:"LDAPS",993:"IMAPS",
        995:"POP3S",1433:"MSSQL",1521:"Oracle",1645:"RADIUS",1646:"RADIUS",
        1812:"RADIUS",1813:"RADIUS-Acct",2049:"NFS",3268:"GC",3269:"GC-SSL",
        3306:"MySQL",3389:"RDP",5060:"SIP",5061:"SIP-TLS",5432:"PostgreSQL",
        5985:"WinRM",5986:"WinRM-S",8080:"HTTP-Alt",8443:"HTTPS-Alt",9100:"JetDirect"}
    def svc_label(port):
        nm=SERVNAME.get(port)
        return "%s(%d)"%(nm,port) if nm else str(port)
    def fmt_bw(x):                         # Mbps を平易な小数で(1未満も保持、科学表記回避)
        if x<=0: return "0"
        if x>=1: return ("%.2f"%x).rstrip("0").rstrip(".")
        d=min(max(2-int(math.floor(math.log10(x))),2),12)   # 有効数字3桁ぶんの小数桁
        return ("%.*f"%(d,x)).rstrip("0").rstrip(".") or "0"
    srv_name_by_ip={ip:nm for nm,ip,vn,st in servers}
    svc_name_by_pp={(proto,port):nm for nm,proto,port,fl in svcs}
    def dev_src(ip):                       # クライアント側(発信) -> マスタ名
        if ip in srv_name_by_ip: return srv_name_by_ip[ip]
        if is_inside(ip): return pc_name_by_seg.get(k24(ip))
        return None
    def dev_dst(ip,proto,port):            # サーバ側(宛先) -> マスタ名
        if ip in srv_name_by_ip: return srv_name_by_ip[ip]
        if not is_inside(ip): return svc_name_by_pp.get((proto,port))
        return None
    fmax=collections.defaultdict(float)    # (src,dst,proto,port) -> 最大 Mbps
    with open(CSV,newline="",encoding="utf-8",errors="replace") as fh:
        r=csv.reader(fh); cols=next(r); ix={c:i for i,c in enumerate(cols)}
        Sip=ix["searchSubject.ipAddress"];Pip=ix["peer.ipAddress"]
        Pp=ix["peer.portProtocol.port"];PrS=ix["searchSubject.portProtocol.protocol"]
        Dur=ix["activeDuration"];By=ix["connection.transferBytes"]
        mxi=max(Sip,Pip,Pp,PrS,Dur,By)
        for v in r:
            if len(v)<=mxi: continue
            proto=(v[PrS] or "").upper()
            if proto not in ("TCP","UDP"): continue
            try: port=int(v[Pp])
            except: continue
            if port<=0: continue
            s=dev_src(v[Sip]); d=dev_dst(v[Pip],proto,port)
            if not s or not d or s==d: continue
            try: dur=float(v[Dur])/1000.0       # activeDuration は ミリ秒
            except: dur=0.0
            if dur<=0: continue                 # 接続時間0は速度算出不可のためスキップ
            try: by=float(v[By])
            except: by=0.0
            mbps=by*8.0/dur/1e6
            key=(s,d,proto,port)
            if mbps>fmax[key]: fmax[key]=mbps
    rows=sorted(fmax.items(),key=lambda kv:(-kv[1],kv[0][0],kv[0][1],kv[0][3]))
    with open(FLOWFILE,"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        w.writerow(["[Flow_List]"]+[""]*14)
        w.writerow(["No","Source Device Name","Destination Device Name","TCP/UDP/ICMP",
                    "Service name(Port)","Max. bandwidth(Mbps)",
                    "Manually rouging path settings","Automatic rouging path settings"]+[""]*7)
        for i,((s,d,proto,port),mbps) in enumerate(rows,1):
            w.writerow([i,s,d,proto,svc_label(port),fmt_bw(mbps)]+[""]*9)
    n_flow=len(rows)

print("===== AUTO-DETECTION (manual overrides take precedence) =====")
print("  inside_public : auto=%s  manual=%s  -> used=%s"
      %(sorted(auto_inside),list(MAN_INSIDE),list(INSIDE_PUBLIC)))
print("  dc_regions    : auto=%s  manual=%s  -> used=%s"
      %(sorted(auto_dc),sorted(MAN_DC),sorted(dc_regs)))
print("  spur_regions  : auto=%s  manual=%s  -> used=%s"
      %(sorted(auto_spur),sorted(MAN_SPUR),sorted(spur_regs)))
print("  seeds(hubs)   : %s   name_map(manual)=%s"%(seeds,NAME_MAP))
print("  site_cidrs    : %s  (dc=%s)"%(SITE_CIDRS,sorted(SITE_CIDR_DC)))
print("\n===== SITE GROUPING =====")
print("DC(server) sites [TOP row]:")
for s in dc_sites:
    print("  %-14s code=%-5s regions=%s subnets=%d"%(s,acode[s],site_regs[s],len(site_subnets[s])))
print("Client sites [BOTTOM row]:")
for s in client_sites:
    print("  %-14s code=%-5s regions=%s subnets=%d"%(s,acode[s],sorted(site_regs[s]),len(site_subnets[s])))
print("\n===== SERVER DETECTION (orientation-based) =====")
print("  config: server_min_bytes=%d require_tcp_synack=%s include_udp=%s subnet_min_flows=%d (min_flows=%d)"
      %(SRV_MIN_BYTES,REQ_SYNACK,INC_UDP,THRESH,MINF))
print("  candidate inside server IPs :", n_cand)
print("  adopted servers (1IP=1dev)  :", len(servers))
print("  out-of-scope IPs            :", len(oos),
      "(%s)"%", ".join("%s=%d"%(k,v) for k,v in reason_cnt.items()) if reason_cnt else "")
if DO_SRV: print("   -> out_of_scope_ips.csv :", OOSFILE)
print("\n===== ENDPOINTS (--endpoints %s) ====="%args.endpoints)
print("  servers(inside, 1IP=1dev):", len(servers))
print("  PCs(1 segment=1dev)      :", len(pcs))
print("  internet svc(proto,port) :", len(svcs), "(below-threshold skipped: %d)"%svc_oos)
if servers: print("   server sample:", [n for n,_,_,_ in servers[:5]])
if pcs:     print("   pc sample    :", [n for n,_,_ in pcs[:5]])
if svcs:    print("   svc sample   :", [(n,fl) for n,_,_,fl in svcs[:8]])
infra=len(alldev)-len(servers)-len(pcs)-len(svcs)
print("\nTotal adopted subnets:",len(adopted)," VLANs:101-%d"%(vlan-1))
print("Device count: infra=%d servers=%d pcs=%d svc=%d total=%d"%(infra,len(servers),len(pcs),len(svcs),len(alldev)))
print("Commands written:",len(cmds),"->",OUTFILE)
if not args.no_flow:
    print("Flow rows written:",n_flow,"->",FLOWFILE)
