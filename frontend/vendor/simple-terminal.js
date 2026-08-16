/* MonitorX minimal serial terminal.
 * A dependency-free terminal surface for the VM serial console. It deliberately
 * implements only the API MonitorX uses and does not interpret terminal escape
 * sequences as HTML. Output is always inserted as text.
 */
(function (global) {
    'use strict';

    const decoder = new TextDecoder();

    class Terminal {
        constructor(options) {
            this.options = options || {};
            this._listeners = [];
            this._element = null;
            this._output = '';
        }

        loadAddon(addon) {
            if (addon && typeof addon.activate === 'function') addon.activate(this);
        }

        open(container) {
            const el = document.createElement('div');
            el.className = 'simple-terminal';
            el.tabIndex = 0;
            el.setAttribute('role', 'textbox');
            el.setAttribute('aria-label', 'VM serial console');
            el.style.fontSize = `${Number(this.options.fontSize) || 14}px`;
            el.addEventListener('keydown', event => {
                let data = '';
                if (event.key === 'Enter') data = '\r';
                else if (event.key === 'Backspace') data = '\x7f';
                else if (event.key === 'Tab') data = '\t';
                else if (event.key === 'Escape') data = '\x1b';
                else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) data = event.key;
                else if (event.ctrlKey && event.key.length === 1) data = String.fromCharCode(event.key.toUpperCase().charCodeAt(0) - 64);
                if (data) {
                    event.preventDefault();
                    this._listeners.forEach(listener => listener(data));
                }
            });
            el.addEventListener('paste', event => {
                event.preventDefault();
                const text = event.clipboardData ? event.clipboardData.getData('text') : '';
                if (text) this._listeners.forEach(listener => listener(text));
            });
            container.appendChild(el);
            this._element = el;
            el.focus();
        }

        onData(listener) {
            this._listeners.push(listener);
            return { dispose: () => { this._listeners = this._listeners.filter(item => item !== listener); } };
        }

        write(data) {
            const text = data instanceof Uint8Array ? decoder.decode(data, { stream: true }) : String(data);
            // Drop ANSI control sequences rather than interpreting untrusted
            // guest output. Preserve ordinary whitespace and control text.
            this._output += text.replace(/\x1B(?:[@-_]|\[[0-?]*[ -\/]*[@-~])/g, '');
            if (this._output.length > 200000) this._output = this._output.slice(-150000);
            if (this._element) {
                this._element.textContent = this._output;
                this._element.style.fontSize = `${Number(this.options.fontSize) || 14}px`;
                this._element.scrollTop = this._element.scrollHeight;
            }
        }

        writeln(text) { this.write(`${text}\r\n`); }
        clear() { this._output = ''; if (this._element) this._element.textContent = ''; }
        focus() { if (this._element) this._element.focus(); }
        dispose() { if (this._element) this._element.remove(); this._element = null; this._listeners = []; }
    }

    class FitAddonImpl {
        activate(terminal) { this.terminal = terminal; }
        fit() {
            if (this.terminal && this.terminal._element) {
                this.terminal._element.style.fontSize = `${Number(this.terminal.options.fontSize) || 14}px`;
            }
        }
    }

    global.Terminal = Terminal;
    global.FitAddon = { FitAddon: FitAddonImpl };
})(window);
