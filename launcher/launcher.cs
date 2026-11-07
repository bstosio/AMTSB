// AMT / AMTSB – mały program startowy dla Windows (z ikoną aplikacji).
// Uruchamia program bez czarnego okna konsoli. Gdy biblioteki nie są jeszcze zainstalowane,
// otwiera instalator (AMT.bat / AMTSB.bat) w zwykłym oknie, żeby było widać postęp.
// Budowanie: launcher\build.bat   (wystarczy Windows – kompilator C# jest w .NET Framework 4)
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

#if AMTSB
[assembly: AssemblyTitle("AMTSB – Automatic Movie Translator SuBtitles")]
[assembly: AssemblyProduct("AMTSB")]
#else
[assembly: AssemblyTitle("AMT – Automatic Movie Translator")]
[assembly: AssemblyProduct("AMT")]
#endif
[assembly: AssemblyVersion("1.0.0.0")]

static class Launcher
{
#if AMTSB
    const string Name = "amtsb";
    const string Bat = "AMTSB.bat";
    const string Mods = "['faster_whisper','sentencepiece','pyaudiowpatch','mss','customtkinter','PIL'," +
                        "'winrt.windows.media.ocr','winrt.windows.graphics.imaging','winrt.windows.storage.streams'," +
                        "'winrt.windows.globalization']";
#else
    const string Name = "amt";
    const string Bat = "AMT.bat";
    const string Mods = "['faster_whisper','sentencepiece','imageio_ffmpeg','customtkinter','tkinterdnd2','PIL']";
#endif

    static string Quote(string s) { return "\"" + s + "\""; }

    [STAThread]
    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string home = Environment.GetEnvironmentVariable("AMT_HOME");
        if (string.IsNullOrEmpty(home))
            home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".amt");
        string py = Path.Combine(home, "venv\\Scripts\\python.exe");
        string pyw = Path.Combine(home, "venv\\Scripts\\pythonw.exe");
        string extra = "";
        foreach (string a in args)
            extra += " " + Quote(a);   // np. film przeciągnięty na AMT.exe
        try
        {
            if (File.Exists(pyw) && LibrariesReady(py))
            {
                Start(pyw, Quote(Path.Combine(dir, Name + ".py")) + extra, dir, false);
            }
            else
            {
                string cmd = Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe";
                Start(cmd, "/c \"" + Quote(Path.Combine(dir, Bat)) + extra + "\"", dir, true);
            }
        }
        catch (Exception e)
        {
            MessageBox.Show("Nie udało się uruchomić programu:\n\n" + e.Message, Name.ToUpper(),
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        return 0;
    }

    // Czy w środowisku %USERPROFILE%\.amt są wszystkie potrzebne biblioteki (szybkie sprawdzenie, bez wczytywania).
    static bool LibrariesReady(string py)
    {
        var psi = new ProcessStartInfo(py, "-c \"import importlib.util as u,sys; " +
                                           "sys.exit(any(u.find_spec(m) is None for m in " + Mods + "))\"");
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        using (var p = Process.Start(psi))
        {
            if (!p.WaitForExit(30000))
                return true;
            return p.ExitCode == 0;
        }
    }

    static void Start(string exe, string arguments, string dir, bool window)
    {
        var psi = new ProcessStartInfo(exe, arguments);
        psi.WorkingDirectory = dir;
        psi.UseShellExecute = window;
        psi.CreateNoWindow = !window;
        Process.Start(psi);
    }
}
