// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Source-bound definition candidates over a pinned trusted browser worker.
// The pinned provider runs with browser globals, its own virtual filesystem and
// NoAccessHost. Node's vm is NOT claimed to be a security sandbox.
const fs = require('node:fs');
const vm = require('node:vm');
const { MessageChannel, MessagePort } = require('node:worker_threads');
const { webcrypto } = require('node:crypto');
const { performance } = require('node:perf_hooks');

const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const digest = value => require('node:crypto').createHash('sha256').update(value).digest('hex');
const code = fs.readFileSync(process.argv[3], 'utf8');
if (digest(Buffer.from(code)) !== '703d47989c5132c29a507f1789a2b583b986e9689f4632a1ef1cbcd4d54fb1bd') throw Error('Pinned provider bundle changed');
const workers = [];
const pending = new Map();
let next = 1;

function makeWorker(name, mode, initialData, port) {
    const handlers = new Map();
    const self = {
        name,
        addEventListener(type, listener) {
            if (!handlers.has(type)) handlers.set(type, []);
            handlers.get(type).push(listener);
        },
        removeEventListener(type, listener) {
            handlers.set(type, (handlers.get(type) || []).filter(x => x !== listener));
        },
        postMessage(message) {
            if (message.type === 'browser/newWorker') {
                makeWorker('background-' + workers.length, 'background', message.initialData, message.port);
                return;
            }
            if (mode !== 'foreground') throw Error('Unexpected background host postMessage');
            if (message.method && message.id !== undefined) {
                const result = message.method === 'workspace/configuration'
                    ? message.params.items.map(() => null) : null;
                self.dispatch({ jsonrpc: '2.0', id: message.id, result });
            } else if (message.id !== undefined && pending.has(message.id)) {
                const entry = pending.get(message.id); pending.delete(message.id);
                clearTimeout(entry.timer); entry.resolve(message);
            }
        },
        close() {},
        dispatch(data) {
            for (const listener of [...(handlers.get('message') || [])]) listener({ data });
            if (typeof self.onmessage === 'function') self.onmessage({ data });
        },
    };
    const sandbox = {
        self, MessageChannel, MessagePort, performance, crypto: webcrypto,
        TextEncoder, TextDecoder, URL, URLSearchParams,
        setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
        console: { log() {}, warn() {}, error() {} },
    };
    const context = vm.createContext(sandbox, { name, codeGeneration: { strings: false, wasm: false } });
    vm.runInContext(code, context, { timeout: 10000, filename: 'pinned-pyright.worker.js' });
    workers.push({ self, context });
    self.dispatch({ type: 'browser/boot', mode, ...(initialData ? { initialData, port } : {}) });
    return self;
}

const foreground = makeWorker('foreground', 'foreground');
function notify(method, params) { foreground.dispatch({ jsonrpc: '2.0', method, params }); }
function request(method, params) {
    const id = next++;
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { pending.delete(id); reject(Error('RPC deadline: ' + method)); }, 15000);
        pending.set(id, { resolve, reject, timer });
        foreground.dispatch({ jsonrpc: '2.0', id, method, params });
    });
}
process.on('uncaughtException', error => {
    process.stderr.write(error.message + '\n'); process.exit(2);
});

function providerFs(worker) { return worker.self.app.serviceProvider.fs(); }
function uri(path) { return foreground.app.convertLspUriStringToUri('file://' + path); }
function projectFiles(vfs, path = '/workspace') {
    const result = [];
    for (const entry of vfs.readdirEntriesSync(uri(path))) {
        const child = path + '/' + entry.name;
        if (entry.isDirectory()) result.push(...projectFiles(vfs, child));
        else if (entry.isFile()) result.push(child);
        else throw Error('Unexpected virtual source entry: ' + child);
    }
    return result.sort();
}
function verifySource(worker) {
    const vfs = providerFs(worker);
    const paths = projectFiles(vfs);
    if (JSON.stringify(paths) !== JSON.stringify(Object.keys(payload.files).sort())) throw Error('Virtual source file map mismatch');
    for (const [path, file] of Object.entries(payload.files)) {
        const raw = Buffer.from(vfs.readFileSync(uri(path)));
        if (raw.length !== file.bytes || digest(raw) !== file.sha256) throw Error('Virtual source bytes mismatch: ' + path);
    }
    const denied = [];
    for (const path of ['/etc/passwd', __filename]) {
        if (vfs.existsSync(uri(path))) throw Error('Host-only file visible in virtual source');
        try { vfs.readFileSync(uri(path)); throw Error('Host-only file was read'); }
        catch (error) { if (error.message === 'Host-only file was read') throw error; denied.push({ path, code: error.code || error.name }); }
    }
    return { worker: worker.self.name, files: paths.length, all_bytes_match: true, host_only_reads_denied: denied, ignoreCase: vfs.ignoreCase };
}

(async () => {
    const files = Object.fromEntries(Object.entries(payload.files).map(([path, file]) => [path, file.text === undefined ? '' : file.text]));
    const initialized = await request('initialize', {
        processId: null, rootUri: 'file:///workspace', rootPath: '/workspace',
        workspaceFolders: [{ uri: 'file:///workspace', name: 'workspace' }],
        capabilities: { textDocument: { documentSymbol: { hierarchicalDocumentSymbolSupport: true }, definition: { linkSupport: true }, references: {} }, window: { workDoneProgress: true } },
        initializationOptions: { files },
    });
    if (initialized.error || !initialized.result || typeof initialized.result !== 'object' || !initialized.result.capabilities) throw Error('Invalid initialize result');
    const expectedPaths = Object.keys(files).sort();
    const deadline = performance.now() + 10000;
    while (workers.length !== 2 || workers.some(w => {
        try { const f = providerFs(w); return !f.existsSync(uri(expectedPaths[0])) || !f.existsSync(uri(expectedPaths.at(-1))); }
        catch { return true; }
    })) {
        if (performance.now() > deadline) throw Error('Virtual file-map installation deadline');
        await new Promise(resolve => setTimeout(resolve, 10));
    }
    // InitialFiles is a text map. Install opaque assets through the provider's
    // own Buffer/file APIs in each initialized filesystem before analysis.
    // Do not exclude binary assets or pretend their placeholders are source.
    for (const worker of workers) {
        const vfs = providerFs(worker);
        const ProviderBuffer = vfs.readFileSync(uri(expectedPaths[0])).constructor;
        for (const [path, file] of Object.entries(payload.files)) {
            if (file.base64 !== undefined) vfs.writeFileSync(uri(path), ProviderBuffer.from(file.base64, 'base64'));
        }
    }
    // Software-only path relocation, matching upstream PR1890. Project bytes
    // and the pinned 1.39.8 provider bundle remain unchanged. Retain originals.
    const stubRoots = [];
    for (const worker of workers) {
        const vfs = providerFs(worker);
        const paths = projectFiles(vfs, '/typeshed');
        const manifest = [];
        for (const path of paths) {
            const target = '/typeshed-fallback' + path.slice('/typeshed'.length);
            const raw = vfs.readFileSync(uri(path));
            vfs.mkdirpSync(target.slice(0, target.lastIndexOf('/')));
            if (vfs.existsSync(uri(target))) throw Error('Pre-existing target stub: ' + target);
            vfs.writeFileSync(uri(target), raw);
            const copied = Buffer.from(vfs.readFileSync(uri(target)));
            if (digest(copied) !== digest(Buffer.from(raw))) throw Error('Stub relocation changed bytes');
            manifest.push([path, target, copied.length, digest(copied)]);
        }
        if (paths.length !== 5276) throw Error('Unexpected pinned embedded typeshed file count');
        if (!vfs.existsSync(uri('/typeshed-fallback/stdlib/builtins.pyi')) || !vfs.existsSync(uri('/typeshed-fallback/stdlib/dataclasses.pyi'))) throw Error('Missing relocated standard library');
        stubRoots.push({ worker: worker.self.name, files: manifest.length, manifest_sha256: digest(Buffer.from(JSON.stringify(manifest))) });
    }
    const before = workers.map(verifySource);
    const capabilities = workers.map(w => ({ worker: w.self.name, globals: vm.runInContext('({process:typeof process,require:typeof require,fetch:typeof fetch})', w.context) }));
    notify('initialized', {});
    const preparation = [];
    for (const [path, file] of Object.entries(payload.files)) {
        if (!/\.pyi?$/.test(path)) continue;
        if (file.text === undefined) throw Error('Unsupported non-UTF8 Python input');
        notify('textDocument/didOpen', { textDocument: { uri: 'file://' + path, languageId: 'python', version: 1, text: file.text } });
        const result = await request('textDocument/documentSymbol', { textDocument: { uri: 'file://' + path } });
        preparation.push({ file: path, symbols: result.result?.length, error: result.error });
        if (result.error || !Array.isArray(result.result)) throw Error('Selected-file preparation failure: ' + path);
    }
    function emit(value) { process.stdout.write('CODENIB_SOURCE_DEFINITION_V1 ' + JSON.stringify(value) + '\n'); }
    const initial = { id: 0, ready: true, source_fingerprint: payload.source_fingerprint, before, capabilities, preparation, stubRoots, pid: process.pid };
    emit(initial);
    const input = require('node:readline').createInterface({ input: process.stdin, terminal: false });
    let count = 0;
    for await (const line of input) {
        if (Buffer.byteLength(line) > 10000) throw Error('Actor request cap exceeded');
        const command = JSON.parse(line);
        if (command.action === 'stop') {
            const after = workers.map(verifySource);
            await request('shutdown', null); notify('exit', null);
            const stopped = { id: command.id, stopped: true, queries: count, after, resources: process.memoryUsage() };
            emit(stopped); process.exit(0);
        }
        if (command.action !== 'definition') throw Error('Unsupported actor command');
        if (++count > 2048) throw Error('Prospective actor query cap exceeded');
        const path = '/workspace/' + command.file;
        if (!Object.hasOwn(payload.files, path)) throw Error('Query file outside captured generation');
        const params = { textDocument: { uri: 'file://' + path }, position: { line: command.line, character: command.character }, ...(command.action === 'references' ? { context: { includeDeclaration: command.include_declaration !== false } } : {}) };
        const response = await request('textDocument/' + command.action, params);
        const observed = { id: command.id, request: command, response, source_fingerprint: payload.source_fingerprint };
        emit(observed);
    }
    throw Error('Input EOF before explicit generation shutdown');
})().catch(error => {
    console.error(error.message); process.exit(1);
});
