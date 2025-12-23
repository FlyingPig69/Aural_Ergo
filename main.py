import time
import json
import threading
import requests
from functools import lru_cache
from flask import Flask, render_template_string, Response, stream_with_context, jsonify, request

app = Flask(__name__)

# --- CONFIGURATION ---
DEFAULT_NODE_URL = "https://node.sigmaspace.io"
current_node_url = DEFAULT_NODE_URL

# backend memory state
known_tx_ids = set()
new_transactions = []
latest_blocks = []
data_lock = threading.Lock()


# --- BACKEND ENGINE ---
def fetch_ergo_data():
    """
    Optimized Engine:
    - Uses Set Difference to find strict NEW transactions.
    - Automatically forgets transactions that leave the mempool (mined/dropped).
    - No more 'flush and spike' behavior.
    """
    global known_tx_ids, current_node_url, latest_blocks
    print(f"--- Ergo Node Engine Online ({current_node_url}) ---")

    last_block_fetch = 0

    while True:
        try:
            # 1. FETCH MEMPOOL (Every 1s)
            url = f"{current_node_url}/transactions/unconfirmed"
            try:
                response = requests.get(url, timeout=2)
                if response.status_code == 200:
                    tx_list = response.json()

                    # SAFETY: Limit processing to avoid massive payloads
                    if len(tx_list) > 50: tx_list = tx_list[:50]

                    # Create a set of IDs currently in the mempool
                    current_mempool_ids = set(tx['id'] for tx in tx_list)

                    # Identify strictly NEW transactions (Node has them, we don't)
                    new_ids = current_mempool_ids - known_tx_ids

                    # SYNC MEMORY:
                    # By setting known_tx_ids to current_mempool_ids, we:
                    # 1. Add the new ones.
                    # 2. Automatically 'forget' ones that were mined (no longer in mempool).
                    known_tx_ids = current_mempool_ids

                    # Process only the NEW items for the frontend
                    batch_to_send = []
                    for tx in tx_list:
                        if tx['id'] in new_ids:
                            total_nano = sum(o.get('value', 0) for o in tx.get('outputs', []))
                            erg_amount = total_nano / 1_000_000_000
                            total_assets = sum(len(o.get('assets', [])) for o in tx.get('outputs', []))

                            batch_to_send.append({
                                'id': tx['id'],
                                'amount': f"{erg_amount:.2f}",
                                'value_raw': erg_amount,
                                'asset_count': total_assets,
                                'ts': time.time() * 1000
                            })
                    
                    if batch_to_send:
                        with data_lock:
                            new_transactions.extend(batch_to_send)
            except Exception as e:
                # Network blip, keep going
                pass

            # 2. FETCH BLOCK HEIGHTS (Every 5s)
            if time.time() - last_block_fetch > 5:
                try:
                    b_url = f"{current_node_url}/blocks/lastHeaders/5"
                    b_res = requests.get(b_url, timeout=2)
                    if b_res.status_code == 200:
                        headers = b_res.json()
                        current_heights = sorted([h['height'] for h in headers], reverse=True)
                        with data_lock:
                            latest_blocks = current_heights
                    last_block_fetch = time.time()
                except:
                    pass

        except Exception as e:
            pass

        time.sleep(1)


threading.Thread(target=fetch_ergo_data, daemon=True).start()


# --- HELPERS ---
@lru_cache(maxsize=1000)
def get_token_info(token_id):
    try:
        url = f"{current_node_url}/blockchain/token/byId/{token_id}"
        r = requests.get(url, timeout=2)
        if r.status_code == 200:
            data = r.json()
            return {'name': data.get('name', None), 'decimals': data.get('decimals', 0)}
    except:
        pass
    return {'name': None, 'decimals': 0}


@lru_cache(maxsize=1000)
def ergo_tree_to_address(ergo_tree):
    try:
        url = f"{current_node_url}/utils/ergoTreeToAddress/{ergo_tree}"
        r = requests.get(url, timeout=2)
        if r.status_code == 200:
            try:
                data = r.json()
                if isinstance(data, dict) and 'address' in data: return data['address']
            except:
                pass
            return r.text.strip().strip('"')
    except:
        pass
    return "Unknown"


@lru_cache(maxsize=2000)
def get_box_address(box_id):
    try:
        url = f"{current_node_url}/blockchain/box/byId/{box_id}"
        r = requests.get(url, timeout=2)
        if r.status_code == 200:
            box = r.json()
            return ergo_tree_to_address(box.get('ergoTree'))
    except:
        pass
    return "Unknown Input"


def process_single_tx(tx_data, resolve_inputs=True):
    tx_id = tx_data.get('id')
    senders = []
    if resolve_inputs:
        for inp in tx_data.get('inputs', [])[:2]:
            addr = get_box_address(inp.get('boxId'))
            senders.append(addr)

    receivers = []
    for out in tx_data.get('outputs', []):
        ergo_tree = out.get('ergoTree')
        address = ergo_tree_to_address(ergo_tree)
        val = out.get('value', 0) / 1_000_000_000

        out_tokens = []
        for asset in out.get('assets', []):
            t_info = get_token_info(asset['tokenId'])
            t_name = t_info.get('name')
            if not t_name: t_name = f"{asset['tokenId'][:6]}..."
            raw = asset.get('amount', 0)
            dec = t_info.get('decimals', 0)
            if dec and dec > 0:
                amt = f"{float(raw) / (10 ** dec):,.{min(dec, 4)}f}"
            else:
                amt = str(raw)
            out_tokens.append({'name': t_name, 'amount': amt})

        receivers.append({
            'address': address, 'amount': f"{val:.4f}", 'assets': out_tokens
        })
    return {'id': tx_id, 'senders': senders, 'receivers': receivers}


# --- FLASK ROUTES ---
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/set_node', methods=['POST'])
def set_node():
    global current_node_url
    data = request.json
    new_url = data.get('url', '').strip().rstrip('/')
    if new_url:
        current_node_url = new_url
        return jsonify({'status': 'ok', 'url': current_node_url})
    return jsonify({'error': 'Invalid URL'})


@app.route('/stream')
def stream():
    def event_stream():
        global new_transactions, latest_blocks
        while True:
            with data_lock:
                if new_transactions:
                    for tx in new_transactions:
                        yield f"data: {json.dumps({'type': 'tx', 'data': tx})}\n\n"
                    new_transactions = []
                yield f"data: {json.dumps({'type': 'blocks', 'data': latest_blocks, 'keepalive': True})}\n\n"
            time.sleep(1)

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")


@app.route('/tx_details/<tx_id>')
def get_tx_details(tx_id):
    # 1. Try Mempool
    try:
        url = f"{current_node_url}/transactions/unconfirmed/byTransactionId/{tx_id}"
        r = requests.get(url, timeout=2)
        if r.status_code == 200:
            return jsonify(process_single_tx(r.json(), resolve_inputs=True))
    except:
        pass

    # 2. Try Blockchain History
    try:
        url_hist = f"{current_node_url}/blockchain/transaction/byId/{tx_id}"
        r_hist = requests.get(url_hist, timeout=2)
        if r_hist.status_code == 200:
            return jsonify(process_single_tx(r_hist.json(), resolve_inputs=True))
    except:
        pass

    return jsonify({'error': 'Tx not found in Mempool or History'})


@app.route('/block_details/<height>')
def get_block_details(height):
    try:
        h_url = f"{current_node_url}/blocks/at/{height}"
        h_res = requests.get(h_url, timeout=3)
        if h_res.status_code != 200: return jsonify({'error': 'Block not found'})
        header_ids = h_res.json()
        if not header_ids: return jsonify({'error': 'No header ID found'})
        header_id = header_ids[0]

        tx_url = f"{current_node_url}/blocks/{header_id}/transactions"
        tx_res = requests.get(tx_url, timeout=5)
        if tx_res.status_code != 200: return jsonify({'error': 'Could not fetch block txs'})

        block_data = tx_res.json()
        transactions = block_data.get('transactions', [])

        processed_txs = []
        for tx in transactions[:15]:
            processed_txs.append(process_single_tx(tx, resolve_inputs=True))

        return jsonify({
            'height': height,
            'header_id': header_id,
            'count': len(transactions),
            'txs': processed_txs
        })
    except Exception as e:
        return jsonify({'error': str(e)})


# --- FRONTEND ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Ergo Wireframe Node</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
    <script src="https://unpkg.com/three@0.128.0/examples/js/renderers/CSS2DRenderer.js"></script>
    <script src="https://unpkg.com/three@0.128.0/examples/js/postprocessing/EffectComposer.js"></script>
    <script src="https://unpkg.com/three@0.128.0/examples/js/postprocessing/RenderPass.js"></script>
    <script src="https://unpkg.com/three@0.128.0/examples/js/postprocessing/ShaderPass.js"></script>
    <script src="https://unpkg.com/three@0.128.0/examples/js/postprocessing/UnrealBloomPass.js"></script>
    <script src="https://unpkg.com/three@0.128.0/examples/js/shaders/CopyShader.js"></script>
    <script src="https://unpkg.com/three@0.128.0/examples/js/shaders/LuminosityHighPassShader.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/tone/14.8.49/Tone.js"></script>

    <style>
        body { margin: 0; background: #000; overflow: hidden; font-family: 'Segoe UI', monospace; }

        #app-title {
            position: absolute; top: 15px; left: 25px;
            color: #fff; font-size: 1.5rem; font-weight: 400;
            letter-spacing: 3px; text-transform: uppercase;
            z-index: 2600; pointer-events: none;
            text-shadow: 0 0 10px rgba(0, 255, 255, 0.3);
        }

        #github-link {
            position: absolute; top: 15px; right: 25px;
            color: #fff; font-size: 12px; text-decoration: none;
            display: flex; align-items: center; z-index: 2600;
            font-family: 'Segoe UI', sans-serif; opacity: 0.8; transition: opacity 0.2s;
        }
        #github-link:hover { opacity: 1.0; text-shadow: 0 0 5px #fff; }
        #github-link svg { fill: white; margin-right: 8px; }

        #top-bar {
            position: absolute; top: 0; left: 0; width: 100%; height: 60px;
            background: rgba(5, 5, 5, 0.95); border-bottom: 1px solid #333;
            display: flex; align-items: center; justify-content: center;
            z-index: 2500;
        }
        .block-item {
            color: #fff; font-size: 1.1rem; cursor: pointer; transition: 0.2s;
            border: 1px solid #222; padding: 6px 14px; border-radius: 4px; margin: 0 8px;
            background: #111; font-weight: bold;
        }
        .block-item:hover { color: #fff; border-color: #00ffff; background: #004444; }
        .block-label { font-size: 0.8rem; color: #00ffff; margin-right: 5px; font-weight: normal; letter-spacing: 1px; }

        #details-panel {
            position: absolute; top: 60px; left: -400px; width: 350px; height: calc(100% - 60px);
            background: rgba(10, 10, 12, 0.98); border-right: 1px solid #00ffff;
            z-index: 2000; transition: left 0.4s cubic-bezier(0.2, 0.8, 0.2, 1);
            color: #fff; padding: 20px; box-sizing: border-box; overflow-y: auto;
            box-shadow: 10px 0 50px rgba(0, 255, 255, 0.1);
        }
        #details-panel.open { left: 0; }

        #recent-panel {
            position: absolute; top: 60px; right: 0; width: 300px; height: calc(100% - 60px);
            background: rgba(5, 5, 8, 0.85); border-left: 1px solid #00ffff;
            z-index: 1500; display: flex; flex-direction: column; pointer-events: auto;
        }

        .panel-header { font-size: 1.5rem; color: #00ffff; margin-bottom: 20px; border-bottom: 1px solid #333; padding-bottom: 10px; }
        .close-btn { float: right; cursor: pointer; color: #666; font-size: 1rem; }
        .close-btn:hover { color: #fff; }
        .section-title { font-size: 0.8rem; color: #888; text-transform: uppercase; letter-spacing: 2px; margin-top: 20px; margin-bottom: 10px; }
        .addr-box { background: rgba(255,255,255,0.05); padding: 10px; border-radius: 4px; font-family: monospace; font-size: 0.9rem; margin-bottom: 5px; border-left: 2px solid #444; }
        .addr-box.to { border-left-color: #00ff88; }
        .addr-box.from { border-left-color: #ff0055; }
        .asset-tag { display: inline-block; background: #222; border: 1px solid #444; padding: 2px 6px; font-size: 0.75rem; margin: 2px; border-radius: 3px; color: #eebb00; }

        .recent-header { padding: 20px; background: rgba(0,255,255,0.1); border-bottom: 1px solid #00ffff; color: #00ffff; font-size: 1.2rem; text-transform: uppercase; letter-spacing: 2px; }
        #tx-list { flex: 1; overflow-y: auto; overflow-x: hidden; }
        .list-item { padding: 15px; border-bottom: 1px solid #222; cursor: pointer; transition: 0.2s; position: relative; animation: slideInRight 0.5s ease-out; }
        .list-item:hover { background: rgba(0,255,255,0.1); padding-left: 20px; }
        .list-amount { color: #fff; font-size: 1.1rem; font-weight: bold; }
        .list-assets { display: inline-block; margin-left: 8px; font-size: 0.75rem; color: #ff00ff; background: rgba(255,0,255,0.1); padding: 2px 5px; border-radius: 4px; border: 1px solid #ff00ff; font-weight: normal; vertical-align: middle; }
        .list-hash { color: #666; font-size: 0.75rem; margin-top: 4px; font-family: monospace; }
        .time-ago { font-size: 0.7rem; color: #aaa; position: absolute; top: 15px; right: 15px; font-weight: bold; }

        #overlay {
            position: absolute; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(5, 5, 10, 0.98); display: flex; flex-direction: column; align-items: center; justify-content: center; 
            z-index: 3000; transition: opacity 1s ease;
        }
        h1 { color: #fff; font-weight: 300; letter-spacing: 10px; font-size: 2.5rem; text-transform: uppercase; margin-bottom: 20px; border-bottom: 1px solid #444; padding-bottom: 10px; }
        .btn-group { display: flex; gap: 20px; margin-top: 20px; }
        .node-input-group { display: flex; flex-direction: column; align-items: center; margin-bottom: 20px; }
        input[type="text"] { background: rgba(255,255,255,0.1); border: 1px solid #444; color: #fff; padding: 10px; width: 300px; text-align: center; font-family: monospace; border-radius: 4px; margin-top: 10px; }
        input[type="text"]:focus { outline: none; border-color: #00ffff; }
        label { color: #888; font-size: 0.8rem; letter-spacing: 1px; }
        button { background: transparent; color: #fff; border: 1px solid #fff; padding: 15px 30px; font-size: 14px; letter-spacing: 2px; cursor: pointer; text-transform: uppercase; transition: 0.2s; min-width: 200px; }
        button:hover { background: #fff; color: #000; }
        button:disabled { border-color: #444; color: #666; cursor: not-allowed; }
        #force-btn { border-color: #ff4444; color: #ff4444; display: none; }
        #status { color: #888; font-size: 12px; margin-top: 15px; letter-spacing: 1px; }

        .tx-marker-wrapper { position: absolute; pointer-events: none; }
        .tx-marker-content { font-family: 'Courier New', monospace; font-weight: bold; color: #00ffff; text-align: center; opacity: 0; transform-origin: center bottom; animation: floatUp 8s forwards ease-out; cursor: pointer; pointer-events: auto; transition: 0.2s; }
        .tx-marker-content:hover { transform: scale(1.2); z-index: 1000; }
        .tx-amount { font-size: 18px; background: rgba(0,0,0,0.8); padding: 4px 8px; border: 1px solid #00ffff; box-shadow: 0 0 15px #00ffff; }
        .tx-hash { font-size: 10px; color: #aaa; margin-top: 2px; background: #000; display: inline-block; padding: 2px; }
        @keyframes floatUp { 0% { opacity: 0; transform: translateY(0px) scale(0.8); } 10% { opacity: 1; transform: translateY(-20px) scale(1.1); } 100% { opacity: 0; transform: translateY(-100px) scale(1.0); } }

        #test-controls { position: absolute; bottom: 30px; left: 30px; z-index: 4000; }
        button.test-btn { background: #000; border: 1px solid #333; color: #666; padding: 10px 20px; font-size: 0.8rem; min-width: auto; }
        button.test-btn:hover { border-color: #fff; color: #fff; box-shadow: 0 0 10px rgba(255,255,255,0.2); }
    </style>
</head>
<body>
    <div id="app-title">Sounds of Ergo</div>

    <a href="https://github.com/FlyingPig" target="_blank" id="github-link">
        <svg height="20" width="20" viewBox="0 0 16 16">
            <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"></path>
        </svg>
        Flying Pig
    </a>

    <div id="top-bar"><span style="color:#00ffff; margin-right:15px; font-weight:bold; letter-spacing:2px;">LATEST BLOCKS:</span><div id="block-list" style="display:flex;"></div></div>
    <div id="details-panel"><div class="panel-header">DETAILS <span class="close-btn" onclick="closePanel()">×</span></div><div id="panel-content"><div style="text-align:center; margin-top:50px; color:#666;">Select a transaction or block...</div></div></div>
    <div id="recent-panel"><div class="recent-header">Activity Feed</div><div id="tx-list"></div></div>

    <div id="overlay">
        <h1>Ergo Node Sync</h1>
        <div class="node-input-group"><label>NODE URL</label><input type="text" id="node-url" value="https://node.sigmaspace.io" placeholder="https://node.sigmaspace.io/"></div>
        <div class="btn-group"><button id="start-btn" onclick="runInitSequence()">INITIALIZE SYSTEM</button></div>
        <div id="status">Waiting for user...</div>
    </div>

    <div id="test-controls"><button class="test-btn" onclick="testTx()">TEST SIGNAL</button></div>
    <div id="world"></div>

    <script>
        function cleanTruncate(str) {
            if (!str || str.toLowerCase() === "unknown input") return "Unknown Input";
            const clean = str.replace(/["'{}]/g, "").trim();
            if (clean.length <= 10) return clean;
            return clean.substring(0, 5) + "..." + clean.substring(clean.length - 3);
        }

        async function updateNodeUrl() {
            const url = document.getElementById('node-url').value;
            await fetch('/set_node', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: url }) });
        }
        function closePanel() { document.getElementById('details-panel').classList.remove('open'); }

        function updateBlockBar(heights) {
            const div = document.getElementById('block-list');
            div.innerHTML = heights.map(h => `<div class="block-item" onclick="fetchBlockDetails(${h})"><span class="block-label">#</span>${h}</div>`).join('');
        }

        setInterval(() => {
            const now = Date.now();
            document.querySelectorAll('.list-item').forEach(item => {
                const tsStr = item.getAttribute('data-ts');
                if(!tsStr) return;
                const ts = parseInt(tsStr);
                const diffMs = now - ts;
                const diffSec = Math.floor(diffMs / 1000);
                const diffMin = Math.floor(diffSec / 60);
                const tag = item.querySelector('.time-ago');
                if(tag) {
                    if(diffSec < 60) tag.innerText = `${diffSec}s ago`;
                    else tag.innerText = `${diffMin}m ago`;
                }
            });
        }, 1000); 

        // QUEUE SYSTEM
        let txQueue = [];
        setInterval(() => {
            if(txQueue.length > 0) {
                if(txQueue.length > 50) { txQueue = txQueue.slice(0, 10); }
                const tx = txQueue.shift();
                playTxSolo(parseFloat(tx.amount)); 
                showTxOnGlobe(tx); 
                addToRecentList(tx);
            }
        }, 200); 

        function addToQueue(tx) { txQueue.push(tx); }

        // --- NEW: Processed Set to prevent duplicate rendering in frontend ---
        const processedTxIds = new Set();

        function addToRecentList(tx) {
            if(tx.id && tx.id.startsWith("TEST_")) {
                // allow test
            } else {
                // Check for duplicates
                if (processedTxIds.has(tx.id)) return;
                processedTxIds.add(tx.id);
                
                // Keep the browser memory clean
                if (processedTxIds.size > 2000) processedTxIds.clear();
            }

            const list = document.getElementById('tx-list');
            const item = document.createElement('div');
            item.className = 'list-item';
            const ts = tx.ts || Date.now();
            item.setAttribute('data-ts', ts);

            let assetHtml = '';
            if(tx.asset_count && tx.asset_count > 0) {
                assetHtml = `<span class="list-assets">+${tx.asset_count} assets</span>`;
            }

            item.innerHTML = `
                <div class="list-amount">${tx.amount} ERG ${assetHtml}</div>
                <div class="list-hash">${cleanTruncate(tx.id)}</div>
                <div class="time-ago">0s ago</div>
            `;
            item.onclick = () => fetchTxDetails(tx.id);
            list.prepend(item);
            if(list.children.length > 15) list.lastChild.remove();
        }

        async function fetchTxDetails(txId) {
            const panel = document.getElementById('details-panel');
            const content = document.getElementById('panel-content');
            panel.classList.add('open');
            content.innerHTML = '<div style="text-align:center; margin-top:50px;">Querying Node...</div>';
            try {
                const res = await fetch(`/tx_details/${txId}`);
                const data = await res.json();
                if(data.error) { content.innerHTML = `<div style="color:red; margin-top:20px; text-align:center;">Error: ${data.error}</div>`; return; }

                let html = `<div class="section-title">Transaction ID</div><div class="addr-box" style="color:#aaa">${cleanTruncate(data.id)}</div>
                    <div class="section-title">From</div>${data.senders.map(s => `<div class="addr-box from">${cleanTruncate(s)}</div>`).join('')}
                    <div class="section-title">To</div>${data.receivers.map(r => `
                        <div class="addr-box to"><div style="color:#00ff88; font-weight:bold; margin-bottom:5px;">${r.amount} ERG</div>
                        <div>${cleanTruncate(r.address)}</div>${r.assets.length > 0 ? '<div style="margin-top:5px; border-top:1px solid #333; padding-top:4px;">' + r.assets.map(a => `<span class="asset-tag">${a.amount} ${a.name}</span>`).join('') + '</div>' : ''}</div>`).join('')}`;
                content.innerHTML = html;
            } catch(e) { content.innerHTML = "<div style='text-align:center; color:red; margin-top:50px;'>Node Connection Failed</div>"; }
        }

        async function fetchBlockDetails(height) {
            const panel = document.getElementById('details-panel');
            const content = document.getElementById('panel-content');
            panel.classList.add('open');
            content.innerHTML = `<div style="text-align:center; margin-top:50px;">Fetching Block ${height}...<br><span style="font-size:10px;color:#666">Resolving input addresses...</span></div>`;
            try {
                const res = await fetch(`/block_details/${height}`);
                const data = await res.json();
                if(data.error) { content.innerHTML = `<div style="color:red; margin-top:20px; text-align:center;">Error: ${data.error}</div>`; return; }

                let html = `<div class="section-title">BLOCK ${data.height}</div><div style="margin:10px 0; color:#888;">Contains ${data.count} Transactions</div>`;
                data.txs.forEach(tx => {
                    html += `<div style="border-top:1px dashed #333; margin-top:15px; padding-top:10px;">
                                <div style="color:#00ffff; font-family:monospace; font-size:0.8rem; margin-bottom:5px;">TX: ${cleanTruncate(tx.id)}</div>
                                <div style="font-size:0.7rem; color:#ff0055; margin-bottom:5px;">FROM: ${tx.senders.map(s => cleanTruncate(s)).join(', ')}</div>
                                ${tx.receivers.map(r => `
                                    <div class="addr-box to" style="margin-top:2px;">
                                        <div style="color:#00ff88; font-weight:bold;">${r.amount} ERG</div>
                                        <div>to: ${cleanTruncate(r.address)}</div>
                                         ${r.assets.length > 0 ? '<div style="margin-top:2px;">' + r.assets.map(a => `<span class="asset-tag">${a.amount} ${a.name}</span>`).join('') + '</div>' : ''}
                                    </div>
                                `).join('')}
                             </div>`;
                });
                if(data.count > 15) html += `<div style="text-align:center; margin-top:10px; color:#666;">... and ${data.count - 15} more ...</div>`;
                content.innerHTML = html;
            } catch(e) { content.innerHTML = "<div style='text-align:center; color:red; margin-top:50px;'>Fetch Failed</div>"; }
        }

        // --- WARM PAD AUDIO ENGINE ---
        let drumKit, hatKit, padSynthA, padSynthB, piano, shimmerSynth, blockPad;
        let reverb, delay, wide, tremolo;
        let isAudioReady = false;
        let lastBlockHeight = 0;
        let kickPulse = 1.0;

        // CHORDS: Lower Octave (2)
        const CHORDS = [
            { root: "C2", notes: ["C3", "Eb3", "G3", "Bb3"], scale: ["C5", "D5", "Eb5", "F5", "G5", "Bb5", "C6"] },
            { root: "Ab1", notes: ["Ab2", "C3", "Eb3", "G3"], scale: ["C5", "Eb5", "F5", "G5", "Ab5", "Bb5"] },
            { root: "F1", notes: ["F2", "Ab2", "C3", "Eb3"], scale: ["F5", "G5", "Ab5", "Bb5", "C6", "Eb6"] },
            { root: "G1", notes: ["G2", "B2", "D3", "F3"], scale: ["G5", "Ab5", "B5", "D6", "F5", "G5"] }
        ];
        let currentChordIndex = 0;

        async function runInitSequence() {
            document.getElementById('start-btn').innerText = "CONNECTING...";
            document.getElementById('start-btn').disabled = true;
            document.getElementById('status').innerText = "Starting Audio Engine...";

            await Tone.start();
            Tone.Transport.bpm.value = 80;

            const loadTimeout = setTimeout(() => {
                if(!isAudioReady) {
                    forceSynthMode();
                }
            }, 3500);

            try {
                // FX
                const limiter = new Tone.Limiter(-1).toDestination();
                reverb = new Tone.Reverb({ decay: 10, wet: 0.5 }).connect(limiter);
                delay = new Tone.PingPongDelay("4n", 0.5).connect(reverb);
                wide = new Tone.Chorus(2, 4, 0.5).connect(reverb);
                tremolo = new Tone.Tremolo(4, 0.2).connect(wide).start();

                // 1. PULSE (Membrane + Lowpass)
                drumKit = new Tone.MembraneSynth({
                    envelope: { attack: 0.001, decay: 0.4, sustain: 0.01, release: 1.4 }
                });
                const drumFilter = new Tone.Filter(100, "lowpass").connect(limiter);
                drumKit.connect(drumFilter);
                drumKit.volume.value = 4; // Boost post-filter

                hatKit = new Tone.MetalSynth({
                    frequency: 200, envelope: { attack: 0.001, decay: 0.05, release: 0.01 },
                    harmonicity: 5.1, modulationIndex: 32, resonance: 4000, octaves: 1.5
                }).connect(reverb);
                hatKit.volume.value = -25;

                // 2. WARM PAD (FatSine + Tremolo + Wide + Reverb)
                padSynthA = new Tone.PolySynth(Tone.Synth, {
                    oscillator: { type: "fatsine", count: 3, spread: 30 }, // Pure, no buzz
                    envelope: { attack: 4, decay: 4, sustain: 1, release: 6 }, // Slow cloud
                    maxPolyphony: 6
                }).connect(tremolo);
                padSynthA.volume.value = -6;

                // 3. PIANO (Real Samples) + SHIMMER
                piano = new Tone.Sampler({
                    urls: { "C4": "C4.mp3", "D#4": "Ds4.mp3", "F#4": "Fs4.mp3", "A4": "A4.mp3" },
                    baseUrl: "https://tonejs.github.io/audio/salamander/",
                }).connect(reverb);
                piano.volume.value = -3; 

                shimmerSynth = new Tone.PolySynth(Tone.Synth, {
                    oscillator: { type: "sine" },
                    envelope: { attack: 0.05, decay: 0.5, sustain: 0.1, release: 2 },
                    maxPolyphony: 4
                }).connect(delay); 
                shimmerSynth.volume.value = -14;

                // 4. BLOCK FOUND (Deep Swell - Filtered Low)
                blockPad = new Tone.PolySynth(Tone.Synth, {
                    oscillator: { type: "fatsawtooth", count: 3, spread: 20 },
                    envelope: { attack: 3, decay: 3, sustain: 1, release: 8 },
                    maxPolyphony: 6
                }).connect(reverb);
                const blockFilter = new Tone.Filter(200, "lowpass").connect(reverb);
                blockPad.disconnect();
                blockPad.connect(blockFilter);
                blockPad.volume.value = -2;

                await Tone.loaded();
                clearTimeout(loadTimeout);
                startExperience();

            } catch (e) {
                console.warn("Audio Setup Error:", e);
                forceSynthMode();
            }
        }

        function forceSynthMode() {
            if(isAudioReady) return;
            // Basic fallbacks
            if(!drumKit) drumKit = new Tone.MembraneSynth().toDestination();
            if(!padSynthA) padSynthA = new Tone.PolySynth().toDestination();
            if(!piano) piano = new Tone.PolySynth().toDestination();
            startExperience();
        }

        function startExperience() {
            isAudioReady = true;
            setupSequencer();
            updateNodeUrl();
            document.getElementById('overlay').style.opacity = 0;
            setTimeout(() => { document.getElementById('overlay').style.display = 'none'; }, 1000);
            initSSE();
        }

        function setupSequencer() {
            // Pulse: Heavy on 1
            const kickSeq = new Tone.Sequence((time, note) => {
                drumKit.triggerAttack(note, time);
                Tone.Draw.schedule(() => { kickPulse = 1.08; }, time);
            }, ["C1", null, "C1", null, "C1", null, "C1", null], "8n").start(0);

            const hatSeq = new Tone.Sequence((time, note) => {
                if(note) hatKit.triggerAttack("32n", time, 0.5);
            }, [null, "x", "x", "x"], "16n").start(0);

            // CHANGED: 2m instead of 4m for faster progression
            const chordLoop = new Tone.Loop(time => {
                const chord = CHORDS[currentChordIndex];

                // Play chord
                padSynthA.triggerAttackRelease(chord.notes, "4m", time);

                // Add Deep Bass Note from Piano
                piano.triggerAttack(chord.root, time, 0.6);

                currentChordIndex = (currentChordIndex + 1) % CHORDS.length;
            }, "2m").start(0);

            Tone.Transport.start();
        }

        function playTxSolo(amount) {
            if (!isAudioReady) return;
            const now = Tone.now();
            const notes = amount > 100 ? 5 : 3;
            const scale = CHORDS[currentChordIndex].scale;
            let t = now;
            for(let i=0; i<notes; i++) {
                const n = scale[Math.floor(Math.random() * scale.length)];
                // Play soft piano
                piano.triggerAttack(n, t, 0.55); 
                // Add halo
                if(shimmerSynth) shimmerSynth.triggerAttackRelease(n, "8n", t);
                t += 0.25; 
            }
        }

        function checkNewBlock(heights) {
            if(heights.length > 0) {
                const newest = heights[0];
                if(newest > lastBlockHeight && lastBlockHeight !== 0) {
                    // Deep Ocean Swell
                    if(blockPad) {
                        blockPad.triggerAttackRelease(["C2", "G2", "C3", "G3"], "8m", Tone.now());
                        // Deep Piano Strike
                        piano.triggerAttack("C1", Tone.now(), 0.9);
                    }
                }
                lastBlockHeight = newest;
            }
        }

        function initSSE() { 
            const evtSource = new EventSource("/stream"); 
            evtSource.onmessage = (e) => { 
                const msg = JSON.parse(e.data);
                if(msg.type === 'tx') {
                    addToQueue(msg.data);
                } else if(msg.type === 'blocks') {
                    checkNewBlock(msg.data);
                    updateBlockBar(msg.data);
                }
            }; 
        }

        function testTx() { 
            const fakeAmt = (Math.random() * 500).toFixed(2); 
            const fakeData = { amount: fakeAmt, id: "TEST_" + Date.now(), ts: Date.now(), asset_count: 3 };
            addToQueue(fakeData); 
        }

        let scene, camera, renderer, labelRenderer, earth, composer, sigmaMesh;
        function initVisuals() {
            scene = new THREE.Scene(); scene.background = new THREE.Color(0x020205);
            camera = new THREE.PerspectiveCamera(45, window.innerWidth/window.innerHeight, 1, 1000); camera.position.z = 180;
            const dirLight = new THREE.DirectionalLight(0xffffff, 1); dirLight.position.set(100, 50, 50); scene.add(dirLight);

            renderer = new THREE.WebGLRenderer({ antialias: true }); renderer.setSize(window.innerWidth, window.innerHeight); document.getElementById('world').appendChild(renderer.domElement);
            labelRenderer = new THREE.CSS2DRenderer(); labelRenderer.setSize(window.innerWidth, window.innerHeight); labelRenderer.domElement.style.position = 'absolute'; labelRenderer.domElement.style.top = '0px'; labelRenderer.domElement.style.pointerEvents = 'none'; document.getElementById('world').appendChild(labelRenderer.domElement);

            const geometry = new THREE.SphereGeometry(55, 64, 64);
            const texLoader = new THREE.TextureLoader();
            const mapTex = texLoader.load('https://upload.wikimedia.org/wikipedia/commons/thumb/b/ba/The_earth_at_night.jpg/1024px-The_earth_at_night.jpg');
            const material = new THREE.MeshBasicMaterial({ 
                map: mapTex, 
                transparent: true, 
                opacity: 0.9,
                color: 0x88ccff, // Cyber blue tint
                blending: THREE.AdditiveBlending 
            });
            earth = new THREE.Group(); 
            earth.add(new THREE.Mesh(geometry, material)); 
            scene.add(earth);

            const sigmaShape = new THREE.Shape();
            sigmaShape.moveTo(10, 10); sigmaShape.lineTo(-10, 10); sigmaShape.lineTo(-2, 0); 
            sigmaShape.lineTo(-10, -10); sigmaShape.lineTo(10, -10); 
            sigmaShape.lineTo(10, -8); sigmaShape.lineTo(-6, -8); sigmaShape.lineTo(0, 0); 
            sigmaShape.lineTo(-6, 8); sigmaShape.lineTo(10, 8);
            const extrudeSettings = { depth: 4, bevelEnabled: true, bevelSegments: 2, steps: 2, bevelSize: 0.5, bevelThickness: 0.5 };
            const sigmaGeo = new THREE.ExtrudeGeometry(sigmaShape, extrudeSettings);
            sigmaGeo.center(); sigmaGeo.scale(1.2, 1.2, 1.2); 
            const sigmaMat = new THREE.MeshPhongMaterial({ color: 0xffffff, emissive: 0x888888, shininess: 100 });
            sigmaMesh = new THREE.Mesh(sigmaGeo, sigmaMat); scene.add(sigmaMesh);

            const bloomPass = new THREE.UnrealBloomPass(new THREE.Vector2(window.innerWidth, window.innerHeight), 1.5, 0.4, 0.85);
            composer = new THREE.EffectComposer(renderer); composer.addPass(new THREE.RenderPass(scene, camera)); composer.addPass(bloomPass);

            window.addEventListener('resize', () => { camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix(); renderer.setSize(window.innerWidth, window.innerHeight); composer.setSize(window.innerWidth, window.innerHeight); labelRenderer.setSize(window.innerWidth, window.innerHeight); });
            animate();
        }

        function animate() {
            requestAnimationFrame(animate);
            const time = Date.now() * 0.001;
            if(earth) earth.rotation.y += 0.001; 
            if(sigmaMesh) {
                 sigmaMesh.rotation.y = Math.sin(time) * 0.35;
                 kickPulse = THREE.MathUtils.lerp(kickPulse, 1.0, 0.08);
                 sigmaMesh.scale.set(1.2 * kickPulse, 1.2 * kickPulse, 1.2 * kickPulse);
            }
            composer.render(); labelRenderer.render(scene, camera);
        }

        function showTxOnGlobe(tx) {
            const wrapper = document.createElement('div'); wrapper.className = 'tx-marker-wrapper';
            const content = document.createElement('div'); content.className = 'tx-marker-content';
            content.innerHTML = `<div class="tx-amount">${tx.amount} ERG</div><div class="tx-hash">${cleanTruncate(tx.id)}</div>`;
            content.onclick = function() { fetchTxDetails(tx.id); }; wrapper.appendChild(content);
            const phi = Math.acos(-1 + (2 * Math.random())); const theta = Math.sqrt(Math.PI * 50) * phi;
            const labelObj = new THREE.CSS2DObject(wrapper); labelObj.position.setFromSphericalCoords(52, phi, theta); earth.add(labelObj);
            setTimeout(() => {
                earth.remove(labelObj);
                wrapper.remove();
            }, 8000);
        }

        initVisuals();
        preloadAudio();
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    app.run(debug=True, threaded=True, port=5000)
