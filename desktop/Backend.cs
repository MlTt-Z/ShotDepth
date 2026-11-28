using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace ShotDepth.Desktop;

internal sealed class Backend : IDisposable
{
    private readonly Process _process;
    private readonly SafeFileHandle _job;
    private readonly TaskCompletionSource<Uri> _ready = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly Queue<string> _errors = new();
    private bool _disposed;

    public Task<Uri> Ready => _ready.Task;
    public bool HasExited => _process.HasExited;
    public event Action? Exited;
    public string Diagnostics
    {
        get { lock (_errors) return string.Join(Environment.NewLine, _errors); }
    }

    public Backend(string directory)
    {
        string editor = Path.Combine(directory, "editor.py");
        string python = Path.Combine(directory, "python", "python.exe");
        if (!File.Exists(python) || !File.Exists(editor))
            throw new FileNotFoundException("发行目录缺少内置 Python 或编辑器，请从完整发行目录启动。");
        var start = new ProcessStartInfo(python)
        {
            WorkingDirectory = directory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (string arg in new[] { "-X", "utf8", "-B", "-u", editor, "--port", "0" })
            start.ArgumentList.Add(arg);
        _job = CreateKillOnCloseJob();
        _process = new Process { StartInfo = start, EnableRaisingEvents = true };
        _process.OutputDataReceived += (_, e) =>
        {
            if (Uri.TryCreate(e.Data, UriKind.Absolute, out var url) && url.Scheme == "http" &&
                url.Host == "127.0.0.1" && url.Port > 0 && url.AbsolutePath == "/" &&
                url.UserInfo.Length == 0 && url.Query.Length == 0 && url.Fragment.Length == 0 &&
                e.Data == $"http://127.0.0.1:{url.Port}/")
                _ready.TrySetResult(url);
        };
        _process.ErrorDataReceived += (_, e) =>
        {
            if (e.Data is null) return;
            lock (_errors)
            {
                if (_errors.Count >= 12) _errors.Dequeue();
                _errors.Enqueue(e.Data[..Math.Min(e.Data.Length, 512)]);
            }
        };
        _process.Exited += (_, _) =>
        {
            _ready.TrySetException(new InvalidOperationException("本地编辑器进程意外退出。"));
            Exited?.Invoke();
        };
        try
        {
            _process.Start();
            if (!AssignProcessToJobObject(_job, _process.SafeHandle)) throw LastError();
            _process.BeginOutputReadLine();
            _process.BeginErrorReadLine();
        }
        catch
        {
            try { if (!_process.HasExited) _process.Kill(entireProcessTree: true); }
            catch (InvalidOperationException) { }
            finally { Dispose(); }
            throw;
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _job.Dispose();
        _process.Dispose();
    }

    private static SafeFileHandle CreateKillOnCloseJob()
    {
        const uint killOnJobClose = 0x2000;
        var job = CreateJobObject(IntPtr.Zero, null);
        if (job.IsInvalid) throw LastError();
        var info = new JobLimits { Basic = new BasicLimits { Flags = killOnJobClose } };
        if (SetInformationJobObject(job, 9, ref info, (uint)Marshal.SizeOf<JobLimits>())) return job;
        var error = LastError();
        job.Dispose();
        throw error;
    }

    private static Exception LastError() => new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimits
    {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinWorkingSet, MaxWorkingSet;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobLimits
    {
        public BasicLimits Basic;
        public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemory, PeakJobMemory;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateJobObject(IntPtr attributes, string? name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(SafeFileHandle job, int infoClass, ref JobLimits info, uint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(SafeFileHandle job, SafeProcessHandle process);
}
