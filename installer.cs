using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class AjiangInstaller
{
    private const string Version = "0.9.3";
    private const string PayloadMarker = "AJIANG_RUNTIME_V1";

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern void SHChangeNotify(uint eventId, uint flags, string item1, IntPtr item2);

    [STAThread]
    private static void Main()
    {
        bool created;
        using (Mutex mutex = new Mutex(true, "Local\\AjiangCaptionInstaller-" + Version, out created))
        {
            if (!created)
            {
                MessageBox.Show("阿江字幕正在安装，请等待当前安装完成。", "阿江字幕",
                    MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            RunInstallerWindow();
        }
    }

    private static void RunInstallerWindow()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Form window = new Form
        {
            Text = "安装阿江字幕",
            ClientSize = new Size(380, 110),
            StartPosition = FormStartPosition.CenterScreen,
            FormBorderStyle = FormBorderStyle.FixedDialog,
            MaximizeBox = false,
            MinimizeBox = false,
            ControlBox = false,
            ShowInTaskbar = true,
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath)
        };
        Label status = new Label
        {
            Text = "正在解压离线运行环境，请稍候...",
            AutoSize = false,
            TextAlign = ContentAlignment.MiddleCenter,
            Dock = DockStyle.Fill,
            Font = new Font("Microsoft YaHei UI", 11F)
        };
        window.Controls.Add(status);
        window.Shown += delegate
        {
            BackgroundWorker worker = new BackgroundWorker();
            worker.DoWork += delegate(object sender, DoWorkEventArgs args) { args.Result = Install(); };
            worker.RunWorkerCompleted += delegate(object sender, RunWorkerCompletedEventArgs args)
            {
                window.Close();
                if (args.Error != null)
                {
                    MessageBox.Show("阿江字幕安装失败：\n\n" + args.Error.Message, "阿江字幕",
                        MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return;
                }
                string desktop = (string)args.Result;
                Process.Start(new ProcessStartInfo(desktop) { UseShellExecute = true });
            };
            worker.RunWorkerAsync();
        };
        Application.Run(window);
    }

    private static string Install()
    {
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string installDir = Path.Combine(local, "AjiangCaption", Version);
        string staging = installDir + ".new-" + Process.GetCurrentProcess().Id;
        try
        {
            StopRunningApp(installDir);
            DeleteWithRetry(staging);
            Directory.CreateDirectory(staging);
            using (FileStream package = File.OpenRead(Application.ExecutablePath))
            {
                long payloadLength;
                long payloadStart = FindPayloadStart(package, out payloadLength);
                using (Stream payload = new SliceStream(package, payloadStart, payloadLength))
                using (ZipArchive archive = new ZipArchive(payload, ZipArchiveMode.Read))
                    archive.ExtractToDirectory(staging);
            }
            DeleteWithRetry(installDir);
            Directory.Move(staging, installDir);

            string desktop = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "阿江字幕.exe");
            Stream launcherResource = Assembly.GetExecutingAssembly().GetManifestResourceStream("launcher.exe");
            if (launcherResource == null)
                throw new InvalidDataException("安装包内缺少启动程序。");
            using (Stream launcher = launcherResource)
            using (FileStream output = File.Create(desktop))
                launcher.CopyTo(output);
            SHChangeNotify(0x00002000, 0x0005, desktop, IntPtr.Zero);
            SHChangeNotify(0x08000000, 0x1000, null, IntPtr.Zero);
            return desktop;
        }
        finally
        {
            try { DeleteWithRetry(staging); } catch { }
        }
    }

    private static void StopRunningApp(string installDir)
    {
        foreach (Process process in Process.GetProcessesByName("pythonw"))
        {
            try
            {
                string executable = process.MainModule.FileName;
                if (executable.StartsWith(Path.Combine(installDir, "python"), StringComparison.OrdinalIgnoreCase))
                {
                    process.Kill();
                    process.WaitForExit(5000);
                }
            }
            catch { }
        }
    }

    private static void DeleteWithRetry(string path)
    {
        if (!Directory.Exists(path)) return;
        Exception last = null;
        for (int attempt = 0; attempt < 20; attempt++)
        {
            try { Directory.Delete(path, true); return; }
            catch (Exception error) { last = error; Thread.Sleep(500); }
        }
        throw new IOException("无法清理旧安装目录，请先退出阿江字幕后重试。", last);
    }

    private static long FindPayloadStart(FileStream stream, out long payloadLength)
    {
        byte[] marker = Encoding.ASCII.GetBytes(PayloadMarker);
        byte[] buffer = new byte[1024 * 1024];
        int read = stream.Read(buffer, 0, buffer.Length);
        for (int i = 0; i <= read - marker.Length - 8; i++)
        {
            bool match = true;
            for (int j = 0; j < marker.Length; j++)
                if (buffer[i + j] != marker[j]) { match = false; break; }
            if (!match) continue;
            long length = BitConverter.ToInt64(buffer, i + marker.Length);
            long start = i + marker.Length + 8;
            if (length > 0 && start + length == stream.Length)
            {
                payloadLength = length;
                return start;
            }
        }
        throw new InvalidDataException("安装包数据损坏或不完整。");
    }

    private sealed class SliceStream : Stream
    {
        private readonly Stream source;
        private readonly long start;
        private readonly long length;
        private long position;

        public SliceStream(Stream source, long start, long length)
        {
            this.source = source;
            this.start = start;
            this.length = length;
        }

        public override bool CanRead { get { return true; } }
        public override bool CanSeek { get { return true; } }
        public override bool CanWrite { get { return false; } }
        public override long Length { get { return length; } }
        public override long Position { get { return position; } set { Seek(value, SeekOrigin.Begin); } }

        public override int Read(byte[] buffer, int offset, int count)
        {
            if (position >= length) return 0;
            count = (int)Math.Min(count, length - position);
            source.Position = start + position;
            int read = source.Read(buffer, offset, count);
            position += read;
            return read;
        }

        public override long Seek(long offset, SeekOrigin origin)
        {
            long target = origin == SeekOrigin.Begin ? offset
                : origin == SeekOrigin.Current ? position + offset
                : length + offset;
            if (target < 0 || target > length)
                throw new IOException("尝试读取安装包数据范围之外的内容。");
            position = target;
            return position;
        }

        public override void Flush() { }
        public override void SetLength(long value) { throw new NotSupportedException(); }
        public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
    }
}
