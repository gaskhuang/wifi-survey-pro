/*
 * NetInspect Pro 原生啟動程式（Contents/MacOS/NetInspect Pro）
 *
 * 為什麼不用 shell 腳本 exec python：
 *   macOS 用「主執行檔所在的 bundle」判斷 App 身分。腳本 exec 之後主程式變成
 *   Resources/python/bin/python3，NSBundle.mainBundle 不再是 .app，
 *   Info.plist 的 NSLocationWhenInUseUsageDescription 讀不到 → 定位授權視窗不會出現，
 *   CoreWLAN 回傳的 SSID / BSSID 永遠被系統遮蔽。
 *   這個啟動程式在同一個程序裡內嵌 Python 直譯器，主執行檔就是 App 本身。
 *
 * 等同於：python3 -B -E -s Resources/app/main.py
 */
#include <Python.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fail(PyConfig *cfg, PyStatus st) {
    PyConfig_Clear(cfg);
    if (PyStatus_IsExit(st)) return st.exitcode;
    Py_ExitStatusException(st);
    return 1;
}

int main(int argc, char **argv) {
    char exe[PATH_MAX], real[PATH_MAX];
    uint32_t sz = sizeof exe;
    if (_NSGetExecutablePath(exe, &sz) != 0 || !realpath(exe, real)) {
        fprintf(stderr, "NetInspect Pro: 無法取得執行檔路徑\n");
        return 70;
    }
    /* real = …/NetInspect Pro.app/Contents/MacOS/NetInspect Pro → 取 Contents */
    char contents[PATH_MAX];
    strlcpy(contents, real, sizeof contents);
    for (int i = 0; i < 2; i++) {
        char *s = strrchr(contents, '/');
        if (!s) return 70;
        *s = '\0';
    }
    char home[PATH_MAX], pyexe[PATH_MAX], script[PATH_MAX];
    snprintf(home, sizeof home, "%s/Resources/python", contents);
    snprintf(pyexe, sizeof pyexe, "%s/bin/python3", home);
    snprintf(script, sizeof script, "%s/Resources/app/main.py", contents);

    PyConfig cfg;
    PyConfig_InitPythonConfig(&cfg);
    cfg.use_environment = 0;        /* -E：忽略使用者的 PYTHON* 環境變數 */
    cfg.user_site_directory = 0;    /* -s：不載入使用者 site-packages */
    cfg.write_bytecode = 0;         /* -B：bundle 已預編譯，寫入會破壞簽章 */
    cfg.parse_argv = 0;
    cfg.pathconfig_warnings = 0;

    PyStatus st;
    st = PyConfig_SetBytesString(&cfg, &cfg.home, home);
    if (PyStatus_Exception(st)) return fail(&cfg, st);
    /* sys.executable 指向真正的 python3，子程序（若有）才不會再啟動一個 App */
    st = PyConfig_SetBytesString(&cfg, &cfg.executable, pyexe);
    if (PyStatus_Exception(st)) return fail(&cfg, st);
    st = PyConfig_SetBytesString(&cfg, &cfg.program_name, pyexe);
    if (PyStatus_Exception(st)) return fail(&cfg, st);
    st = PyConfig_SetBytesString(&cfg, &cfg.run_filename, script);
    if (PyStatus_Exception(st)) return fail(&cfg, st);
    /* sys.argv[0] = main.py → sys.path[0] = app 目錄；其餘參數原樣轉交（打包時的 --selftest） */
    char **pargv = calloc((size_t)argc + 1, sizeof(char *));
    if (!pargv) return 71;
    pargv[0] = script;
    for (int i = 1; i < argc; i++) pargv[i] = argv[i];
    st = PyConfig_SetBytesArgv(&cfg, argc, pargv);
    free(pargv);
    if (PyStatus_Exception(st)) return fail(&cfg, st);

    st = Py_InitializeFromConfig(&cfg);
    if (PyStatus_Exception(st)) return fail(&cfg, st);
    PyConfig_Clear(&cfg);
    return Py_RunMain();
}
