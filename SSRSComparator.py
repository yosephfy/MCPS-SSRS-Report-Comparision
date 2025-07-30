# -*- coding: utf-8 -*-
"""
SSRS Comparator GUI Application
--------------------------------

Author:        Yoseph Tezera    Email: masreshayoseph@gmail.com
Last Edited:   2025-07-28
Version:       1.0

Description:
    A standalone tkinter GUI tool to compare SSRS reports across two servers.
    - Loads a CSV of report definitions (name, base URL, optional parameter overrides).
    - For each report, discovers parameters, applies combinations (either from file or discovered),
      renders the report on both servers in parallel, hashes the output rows, and diffs any mismatches.
    - Normalizes whitespace so embedded timestamps do not trigger false mismatches.
    - Saves per-combo raw output and unified diff files under `./output/<ReportName>/`.
    - Presents both user-friendly (INFO/WARN/ERROR) and developer (DEBUG) logs in separate tabs.
    - Bundles ChromeDriver for distribution and handles common transient errors with retries.

Usage:
    1. Place `chromedriver.exe` alongside this script (or let PyInstaller unpack it).
    2. Run `python comparision_tool.py` (or launch the bundled EXE).
    3. In the GUI, click “Load URL List...” and select your CSV.
    4. CSV format (comma-delimited):
         ReportName,BaseURL,ParamLabel1=[opt1;opt2],ParamLabel2=[optA]
       Any parameters you list will override or augment discovered ones.
    5. Choose Server1/Server2 from the dropdowns (e.g. prod, uat, dev).
    6. Click “Compare” to start. Results and diffs are saved under `./output`.

Error Handling:
    - Retries parameter applications up to 3x on timeouts, stale references, or missing elements.
    - Continues to next report on unrecoverable errors (network, missing page, etc.), marking it “ERR”.
    - All exceptions are logged in the developer pane; users see concise status updates.

Dependencies:
    - Python 3.8+
    - selenium
    - tkinter (standard library)
    - difflib, hashlib, threading, urllib.parse (standard library)
    - Chrome browser matching the bundled chromedriver.exe

Packaging:
    - To bundle as a standalone executable, use PyInstaller or similar.  `get_chromedriver_path()` locates the driver at runtime.
"""

import os
import sys
import re
import hashlib
import threading
import time
import datetime
import difflib
import tkinter as tk
from tkinter import messagebox, filedialog, scrolledtext, ttk
from urllib.parse import urlparse, urlunparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.common.exceptions import (
    TimeoutException, StaleElementReferenceException,
    WebDriverException, NoSuchElementException
)
from selenium.webdriver.chrome.service import Service


def get_chromedriver_path():
    # When frozen by PyInstaller, chromedriver.exe lives under sys._MEIPASS
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "chromedriver.exe")
    return os.path.join(os.path.dirname(__file__), "chromedriver.exe")


# Ensure output directory exists
OUTPUT_DIR = os.path.abspath("output")


# ─── LOGGING ABSTRACTION ─────────────────────────────────────────────────────
class Log:
    """Abstracts user and developer logging into two streams."""

    def __init__(self, user_fn, dev_fn):
        self.user_fn = user_fn
        self.dev_fn = dev_fn

    def user(self, message):
        msg = (message
               .replace('[INFO]', '🔍')
               .replace('[WARN]', '⚠️')
               .replace('[ERROR]', '❌')
               .replace('[DEBUG]', ''))
        self.user_fn(msg)

    def dev(self, message):
        self.dev_fn(message)


# ─── APPLY A SINGLE PARAMETER ────────────────────────────────────────────────
def apply_one_parameter(driver, name, value, log):
    """
    Clicks + selects one parameter. Retries up to 3x on transient errors.
    """
    for attempt in range(3):
        try:
            root = driver.find_element(
                By.CSS_SELECTOR, f"div[data-parametername='{name}']")
            log.dev(
                f"[DEBUG] Applying parameter '{name}' -> '{value}' (attempt {attempt+1})")

            # MULTISELECT
            if root.find_elements(By.TAG_NAME, 'button'):
                btn = root.find_element(By.TAG_NAME, 'button')
                btn_id = btn.get_attribute('id')
                drop_id = btn_id.replace('_ctl01', '_divDropDown')
                WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.ID, btn_id)))
                btn.click()
                WebDriverWait(driver, 5).until(
                    lambda d: d.find_element(By.ID, drop_id).is_displayed())
                drop = driver.find_element(By.ID, drop_id)

                if value.lower() == 'all':
                    cb = drop.find_element(
                        By.CSS_SELECTOR, "input[type='checkbox']")
                    if not cb.is_selected():
                        cb.click()
                else:
                    for cb in drop.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
                        cid = cb.get_attribute('id')
                        lbl = driver.find_element(
                            By.CSS_SELECTOR, f"label[for='{cid}']").text.strip()
                        if lbl == value:
                            if not cb.is_selected():
                                cb.click()
                            break

                driver.find_element(By.TAG_NAME, 'body').click()
                WebDriverWait(driver, 5).until(
                    EC.invisibility_of_element_located((By.ID, drop_id)))

            # SINGLE-SELECT
            elif root.find_elements(By.TAG_NAME, 'select'):
                sel = Select(root.find_element(By.TAG_NAME, 'select'))
                sel.select_by_visible_text(value)

            # TEXT/DATE
            else:
                inp = root.find_element(By.CSS_SELECTOR, "input[type='text']")
                driver.execute_script(
                    "arguments[0].value=arguments[1];arguments[0].dispatchEvent(new Event('change'));",
                    inp, value
                )

            return  # success

        except (TimeoutException, StaleElementReferenceException,
                NoSuchElementException, WebDriverException) as e:
            log.dev(
                f"[WARN] apply_one_parameter '{name}' attempt {attempt+1} failed: {e}")
            time.sleep(1)

    # all retries failed
    raise RuntimeError(f"Failed to apply parameter '{name}' after 3 attempts")


# ─── PARAMETER NAME + LABEL DISCOVERY ─────────────────────────────────────────
def discover_parameter_names(driver, log):
    """
    Returns a list of (label_text, parameter_name) tuples.
    """
    mappings = []
    elems = driver.find_elements(By.CSS_SELECTOR, "div[data-parametername]")
    log.dev(f"[DEBUG] Found {len(elems)} parameters for label discovery")
    for root in elems:
        try:
            cid = root.get_attribute('id')
            name = root.get_attribute("data-parametername")
            label = driver.find_element(
                By.CSS_SELECTOR, f"label[for^='{cid}']").text.strip()
            log.dev(f"[DEBUG] Mapped label '{label}' → name '{name}'")
            mappings.append((label, name))
        except Exception as e:
            log.dev(f"[WARN] Skipping parameter root={root}: {e}")
    return mappings


# ─── USER PARAM REMAP ─────────────────────────────────────────────────────────
def remap_user_params(user_params, label_name_map, log):
    """
    Converts user-provided {Label: [values]} into {param_name: [values]}.
    """
    remapped = {}
    label_to_name = {lbl: name for lbl, name in label_name_map}
    for user_label, values in user_params.items():
        if user_label in label_to_name:
            real = label_to_name[user_label]
            remapped[real] = values
            log.dev(f"[DEBUG] Remapped user param '{user_label}' → '{real}'")
        else:
            log.dev(
                f"[WARN] User param label '{user_label}' not found; skipping")
    return remapped


# ─── RENDER & HASH ────────────────────────────────────────────────────────────
def render_and_hash(driver, combo_name, server, report, log, ignore_times=False):
    """
    Renders the report, collects all table rows, normalizes whitespace,
    saves them to output/, and returns a SHA256 hash of the content.
    """
    try:
        log.dev(f"[DEBUG] Rendering '{combo_name}' on {server}")
        driver.find_element(By.CSS_SELECTOR,
                            "input[type='submit'][value='View Report']").click()
        WebDriverWait(driver, 300).until(
            EC.presence_of_all_elements_located((
                By.CSS_SELECTOR,
                "div[id^='VisibleReportContentReportViewerControl'] table tr"
            ))
        )
        rows = driver.execute_script(
            "return Array.from("
            "document.querySelectorAll("
            "\"div[id^='VisibleReportContentReportViewerControl'] table tr\""
            ")).map(tr=>tr.innerText);"
        )
        # normalize whitespace

        lines = [re.sub(r"\s+", " ", r).strip() for r in rows]

        # optionally strip out any time strings like "HH:MM[:SS] AM/PM"
        if ignore_times:
            time_pattern = re.compile(
                r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?\b", re.IGNORECASE)
            lines = [time_pattern.sub("", line) for line in lines]

        lines = ['|'.join(r.split('\n')) for r in lines]
        lines.sort()

        # save to output
        folder = os.path.join(OUTPUT_DIR, report)
        os.makedirs(folder, exist_ok=True)
        safe = combo_name.replace(';', '_')
        path = os.path.join(folder, f"{server}-{safe}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        log.dev(f"[DEBUG] Saved rows → {path}")
        checksum = hashlib.sha256("\n".join(lines).encode()).hexdigest()
        return checksum

    except Exception as e:
        log.dev(
            f"[ERROR] render_and_hash failed for '{combo_name}' on {server}: {e}")
        raise


# ─── DIFF GENERATION ──────────────────────────────────────────────────────────
def generate_diff(report, combo_name, s1, s2, log):
    """
    Generates a unified diff between the two saved combo files and writes it.
    """
    folder = os.path.join(OUTPUT_DIR, report)
    safe = combo_name.replace(';', '_')
    f1 = os.path.join(folder, f"{s1}-{safe}.txt")
    f2 = os.path.join(folder, f"{s2}-{safe}.txt")
    try:
        diff = difflib.unified_diff(
            open(f1, encoding='utf-8').read().splitlines(),
            open(f2, encoding='utf-8').read().splitlines(),
            fromfile=f1, tofile=f2, lineterm=""
        )
        diff_path = os.path.join(folder, f"diff-{safe}.txt")
        with open(diff_path, 'w', encoding='utf-8') as df:
            df.write("\n".join(diff))
        log.dev(f"[DEBUG] Diff saved → {diff_path}")
        return diff_path
    except Exception as e:
        log.dev(f"[ERROR] generate_diff failed: {e}")
        return None


# ─── REPORT CLIENT ────────────────────────────────────────────────────────────
class ReportClient:
    """
    Encapsulates a Selenium ChromeDriver tied to a specific server host.
    """

    def __init__(self, server, domain, report, log):
        self.server = server
        self.report = report
        self.log = log

        opts = webdriver.ChromeOptions()
        opts.add_argument("--headless=new")
        chromedriver = get_chromedriver_path()
        self.log.dev(f"[DEBUG] Using ChromeDriver at {chromedriver}")
        service = Service(chromedriver)
        self.driver = webdriver.Chrome(service=service, options=opts)
        self.domain = domain

    def rs_url(self, base):
        p = urlparse(base)
        host = f"{self.server}.{self.domain}"
        u = urlunparse(p._replace(netloc=host))
        return u.replace('Reports/report/', 'ReportServer?/') + '&rs:Format=HTML4.0'

    def load(self, rs):
        try:
            self.log.dev(f"[DEBUG] Loading {rs}")
            self.driver.get(rs)
            time.sleep(0.5)
        except WebDriverException as e:
            raise RuntimeError(f"Page load failed: {e}")

    def close(self):
        try:
            self.log.dev(f"[DEBUG] Closing browser '{self.server}'")
            self.driver.quit()
        except Exception as e:
            self.log.dev(f"[WARN] Error closing browser: {e}")


# ─── COMPARE LOGIC ────────────────────────────────────────────────────────────
def compare_reports(base_url, s1, s2, report, user_params, log, stop_event, ignore_times=True):
    """
    Compares a single report across two servers:
      - Discovers parameter labels → names.
      - Remaps any user-provided params.
      - Recursively builds combos, applies them, and hashes+diffs.
      - Reports INFO/WARN/ERROR to the GUI.
    """
    domain = urlparse(base_url).netloc.split('.', 1)[1]
    c1 = ReportClient(s1, domain, report, log)
    c2 = ReportClient(s2, domain, report, log)

    try:
        rs1, rs2 = c1.rs_url(base_url), c2.rs_url(base_url)
        log.user(f"[INFO] [REPORT]: {report}")
        log.user(f"  • Server1={rs1}")
        log.user(f"  • Server2={rs2}")
        log.user(f"  • Parameters from file: {user_params or 'NONE'}")

        # Discover label→name mappings
        c1.load(rs1)
        label_map = discover_parameter_names(c1.driver, log)
        params = remap_user_params(user_params, label_map, log)

        # Discover ordered param names
        c1.load(rs1)
        param_names = [n for _, n in discover_parameter_names(c1.driver, log)]

        # Build combos via recursion
        combos = []

        def traverse(idx, combo):
            if stop_event.is_set():
                return  # immediately unwind back up

            if idx >= len(param_names):
                combos.append(combo)
                return
            name = param_names[idx]
            opts = params.get(name, [])
            if not opts:
                # On‐the‐fly read of remaining options
                root = c1.driver.find_element(
                    By.CSS_SELECTOR, f"div[data-parametername='{name}']")
                if root.find_elements(By.TAG_NAME, 'button'):
                    opts = ['all']
                elif root.find_elements(By.TAG_NAME, 'select'):
                    sel = root.find_element(By.TAG_NAME, 'select')
                    opts = [o.text for o in sel.find_elements(
                        By.TAG_NAME, 'option') if o and o.text.lower() != '<select a value>']
                else:
                    val = root.find_element(
                        By.CSS_SELECTOR, "input[type='text']").get_attribute('value') or \
                        datetime.date.today().strftime('%-m/%-d/%Y')
                    opts = [val]

            for v in opts:
                try:
                    apply_one_parameter(c1.driver, name, v, log)
                    traverse(idx+1, combo+[(name, v)])
                except Exception as e:
                    log.dev(f"[ERROR] Skipping combo at param '{name}': {e}")

        traverse(0, [])

        total = len(combos)
        log.user(f"[INFO]   • Total combos: {total}")

        mismatches = []
        errors = []

        for i, combo in enumerate(combos, 1):
            if stop_event.is_set():
                break
            desc = ';'.join(f"{n}={v}" for n, v in combo)
            log.user(f"[INFO]   {i}) {desc}  → Checking")
            res = {}

            def run(client, rs, key):
                if stop_event.is_set():
                    return
                try:
                    client.load(rs)
                    for n, v in combo:
                        apply_one_parameter(client.driver, n, v, log)
                    res[key] = render_and_hash(
                        client.driver, desc, client.server, report, log, ignore_times=ignore_times)
                except Exception as e:
                    log.dev(f"[ERROR] Combo run failed ({client.server}): {e}")
                    res[key] = None

            t1 = threading.Thread(target=run, args=(c1, rs1, 'h1'))
            t2 = threading.Thread(target=run, args=(c2, rs2, 'h2'))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            h1, h2 = res.get('h1'), res.get('h2')
            if h1 is None or h2 is None:
                status = '❌ ERROR'
                errors.append(combo)
            else:
                status = '✅ MATCH' if h1 == h2 else '⚠️ MISMATCH'
                if status == '⚠️ MISMATCH':
                    mismatches.append(combo)
                    diff = generate_diff(report, desc, s1, s2, log)
                    if diff:
                        log.dev(f"[DEBUG] Diff saved → {diff}")

            log.user(f"[INFO]   {i}) {desc}  → {status}")

        # Summary
        if mismatches or errors:
            issues = len(mismatches)
            log.user(f"[INFO]   ↳ Summary: {issues}/{total} mismatches"
                     + (" and errors" if errors else ""))
            return 'ERR' if errors else 'DIFF'
        else:
            log.user(f"[INFO]   ↳ Summary: All matched")
            return 'OK'
    except Exception as e:
        log.user(f"[ERROR] [REPORT]: {report} → Inconclusive due to error")
        log.dev(f"[ERROR] compare_reports({report}) failed: {e}")
        return 'ERR'

    finally:
        log.user(f"\n")
        c1.close()
        c2.close()


# ─── GUI ─────────────────────────────────────────────────────────────────────
class App:
    def __init__(self, root):
        self.data = []
        self.stop_event = threading.Event()  # for cancellation

        # Catch the window‑close (X) event
        root.protocol("WM_DELETE_WINDOW", self.on_closing)

        root.title("SSRS Comparator")
        root.geometry("1200x600")
        root.minsize(1200, 300) 

        # Tabs for logs
        notebook = ttk.Notebook(root)
        notebook.pack(fill='both', expand=True)
        uf, df = ttk.Frame(notebook), ttk.Frame(notebook)
        notebook.add(uf, text='User Logs')
        notebook.add(df, text='Dev Logs')

        # Top controls container
        tf = ttk.Frame(root)
        tf.pack(fill='x', pady=5)

        # --- left side: stacked button+label pairs ---
        left_tf = ttk.Frame(tf)
        left_tf.pack(side='left', anchor='nw', padx=5)

        # 1) Load URL List button + its label
        load_row = ttk.Frame(left_tf)
        load_row.pack(fill='x', pady=(0,5))
        self.load_btn = ttk.Button(load_row, text="Load URL List...", command=self.load)
        self.load_btn.pack(side='left')
        self.lbl = ttk.Label(load_row, text="No file loaded")
        self.lbl.pack(side='left', padx=(5,0))

        # 2) Select Output Folder button + its label
        output_row = ttk.Frame(left_tf)
        output_row.pack(fill='x')
        self.output_btn = ttk.Button(output_row, text="Select Output Folder…", command=self.select_output)
        self.output_btn.pack(side='left')
        self.output_lbl = ttk.Label(output_row, text=OUTPUT_DIR)
        self.output_lbl.pack(side='left', padx=(5,0))


        # Server dropdowns
        vals = ["mcpsdwreporting", "mcpsdwreporting-uat", "mcpsdwreporting-dev", "other"]
        self.s1 = ttk.Combobox(tf, values=vals, state='readonly')
        self.s1.set(vals[0])
        self.s1.pack(side='left', padx=5)
        self.s2 = ttk.Combobox(tf, values=vals, state='readonly')
        self.s2.set(vals[1])
        self.s2.pack(side='left', padx=5)

        # Ignore times checkbox
        self.ignore_times_var = tk.BooleanVar(value=True)
        self.ignore_chk = ttk.Checkbutton(
            tf, text="Ignore Time Strings", variable=self.ignore_times_var
        )
        self.ignore_chk.pack(side='left', padx=5)

        # --- right side: Compare / Stop always visible ---
        right_tf = ttk.Frame(tf)
        right_tf.pack(side='right', anchor='ne', padx=5)

        self.compare_btn = ttk.Button(right_tf, text="Compare", command=self.run)
        self.compare_btn.pack(side='left', padx=(0, 5))

        self.stop_btn = ttk.Button(right_tf, text="Stop", command=self._stop)
        self.stop_btn.pack(side='left')

        # Logs
        self.ul = scrolledtext.ScrolledText(uf, state='disabled', height=20, font=("Ubuntu", 11))
        self.ul.pack(fill='both', expand=True, padx=5, pady=5)
        self.dl = scrolledtext.ScrolledText(df, state='disabled', height=20)
        self.dl.pack(fill='both', expand=True, padx=5, pady=5)

        self.log = Log(self._u, self._d)

        # INITIAL STATE: only Load enabled
        self._set_widgets_state(load=True, compare=False, ignore=False, stop=False, output_folder=True)


    def _set_widgets_state(self, load, compare, ignore, stop, output_folder):
        self.load_btn.config(state='normal' if load else 'disabled')
        self.compare_btn.config(state='normal' if compare else 'disabled')
        self.ignore_chk.config(state='normal' if ignore else 'disabled')
        self.stop_btn.config(state='normal' if stop else 'disabled')
        self.output_btn.config(state='normal' if output_folder else 'disabled')

    def _stop(self):
        """Signal the worker thread to cancel."""
        self.stop_event.set()
        self.log.user("[INFO] Stopping...")

    def _u(self, t):
        self.ul.configure(state='normal')
        self.ul.insert('end', t + "\n")
        self.ul.see('end')
        self.ul.configure(state='disabled')

    def _d(self, t):
        self.dl.configure(state='normal')
        self.dl.insert('end', t + "\n")
        self.dl.see('end')
        self.dl.configure(state='disabled')
    
    def select_output(self):
        global OUTPUT_DIR
        folder = filedialog.askdirectory(
            title="Select output folder", initialdir=OUTPUT_DIR
        )
        if folder:
            OUTPUT_DIR = os.path.abspath(folder)
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            self.output_lbl.config(text=f"Output: {OUTPUT_DIR}")
            self.log.user(f"[INFO] Output folder set to {OUTPUT_DIR}")

    
    def on_closing(self):
        # Signal any running worker to stop
        self.stop_event.set()
        self.log.user("[INFO] GUI closing, stopping background work…")

        # Give threads a moment to clean up WebDrivers
        time.sleep(0.5)

        # Then destroy the window and exit
        root.destroy()

    def load(self):
        path = filedialog.askopenfilename(
            filetypes=[("CSV", "*.csv"), ("All", "*.*")]
        )
        if not path:
            return
        self.data.clear()
        with open(path, encoding='utf-8') as f:
            for line in f:
                cols = [c.strip() for c in line.split(',')]
                if len(cols) < 2: continue
                report, url = cols[0], cols[1]
                params = {}
                for spec in cols[2:]:
                    if '=' not in spec: continue
                    key, raw = spec.split('=', 1); raw = raw.strip()
                    if raw.startswith('[') and raw.endswith(']'):
                        values = [v for v in raw[1:-1].split(';') if v]
                    else:
                        values = [raw]
                    params[key] = values
                self.data.append((report, url, params))

        self.lbl.config(text=os.path.basename(path))
        self.log.user(f"[INFO] Loaded {len(self.data)} reports from {path}")

        # After load: enable Compare & Ignore, disable Stop
        self._set_widgets_state(load=True, compare=True, ignore=True, stop=False, output_folder=True)

    def run(self):
        if not self.data:
            messagebox.showwarning("No URLs", "Load CSV file first")
            return

        # Disable Load, Compare, Ignore; enable Stop
        self._set_widgets_state(load=False, compare=False, ignore=False, stop=True, output_folder=False)
        self.stop_event.clear()

        s1, s2 = self.s1.get(), self.s2.get()
        ignore = self.ignore_times_var.get()

        def worker():
            overall = []
            for report, u, p in self.data:
                if self.stop_event.is_set():
                    break
                st = compare_reports(u, s1, s2, report, p, self.log, stop_event=self.stop_event, ignore_times=ignore)
                overall.append((report, st))
            # Summary
            self.log.user("\n[INFO] Overall Summary:")
            for r, st in overall:
                icon = '✅ OK' if st == 'OK' else '⚠️ DIFF' if st == 'DIFF' else '❌ ERR'
                self.log.user(f"  • {r}: {icon}")
            self.log.user("\n\n\n===============================================\n\n\n")

            # Done (or stopped)—re-enable controls on main thread
            root.after(0, lambda: self._set_widgets_state(
                load=True, compare=True, ignore=True, stop=False, output_folder=True
            ))

        threading.Thread(target=worker, daemon=True).start()


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
