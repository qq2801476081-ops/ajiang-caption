using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class AjiangPythonLauncher
{
    private const string Version = "0.9.3";

    [STAThread]
    private static void Main()
    {
        string installDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "AjiangCaption", Version);
        string appPath = Path.Combine(installDir, "app.py");
        try
        {
            if (!File.Exists(appPath) || ReadVersion(installDir) != Version)
                throw new InvalidOperationException("安装文件不完整，请重新运行阿江字幕安装包。");
            string python = Path.Combine(installDir, "python", "pythonw.exe");
            if (!File.Exists(python))
                throw new InvalidOperationException("未找到内置运行环境，请重新运行安装包。");
            ProcessStartInfo info = new ProcessStartInfo(python, "\"app.py\"");
            info.WorkingDirectory = installDir;
            info.UseShellExecute = false;
            Process.Start(info);
        }
        catch (Exception error)
        {
            MessageBox.Show("阿江字幕启动失败：\n\n" + error.Message, "阿江字幕",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private static string ReadVersion(string installDir)
    {
        string path = Path.Combine(installDir, "version.txt");
        return File.Exists(path) ? File.ReadAllText(path).Trim() : "";
    }

}

