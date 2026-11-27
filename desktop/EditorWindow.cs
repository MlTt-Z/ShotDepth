using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using Microsoft.Win32;

namespace SpikeMark.Desktop;

internal sealed class EditorWindow : Window
{
    private const int MaxLibraryBytes = 1024 * 1024;
    // A JSON string value can escape every byte as a six-character sequence.
    private const int MaxMessageChars = 6 * MaxLibraryBytes + 128;
    private static readonly Encoding Utf8 = new UTF8Encoding(false, true);
    private readonly CancellationTokenSource _closing = new();
    private readonly WebView2 _view = new();
    private readonly TextBlock _status = new() { Text = "正在启动本地编辑器…", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(24) };
    private readonly string _dataDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SpikeMark", "Desktop");
    private string LibraryPath => Path.Combine(_dataDir, "library.json");
    private Backend? _backend;
    private Uri? _origin;
    private string? _libraryScript;
    private string? _library;
    private bool _updatingScript;
    private bool _sceneDirty;
    private bool _confirmingClose;
    private readonly Dictionary<CoreWebView2DownloadOperation, EventHandler<object>> _sceneDownloads = new();

    public EditorWindow()
    {
        Title = "ShotDepth";
        Width = 1360;
        Height = 860;
        MinWidth = 680;
        MinHeight = 480;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        Content = _status;
        Loaded += StartAsync;
        Closing += ConfirmClose;
        Closed += (_, _) =>
        {
            _closing.Cancel();
            try
            {
                foreach (var (operation, handler) in _sceneDownloads)
                    DetachDownloadHandler(operation, handler);
                _sceneDownloads.Clear();
            }
            finally
            {
                try { _view.Dispose(); }
                finally { _backend?.Dispose(); }
            }
        };
    }

    private void ConfirmClose(object? sender, CancelEventArgs args)
    {
        if (_confirmingClose)
        {
            args.Cancel = true;
            return;
        }
        if (args.Cancel || !_sceneDirty) return;

        _confirmingClose = true;
        try
        {
            args.Cancel = MessageBox.Show(this,
                "有未应用的草稿或未导出的场景修改。退出将丢弃这些内容；如需保留，请取消退出，先应用草稿（如有），再导出场景 JSON。",
                "退出 ShotDepth", MessageBoxButton.OKCancel, MessageBoxImage.Warning,
                MessageBoxResult.Cancel) != MessageBoxResult.OK;
        }
        finally { _confirmingClose = false; }
    }

    private async void StartAsync(object sender, RoutedEventArgs e)
    {
        Loaded -= StartAsync;
        try
        {
            Directory.CreateDirectory(_dataDir);
            var env = await CoreWebView2Environment.CreateAsync(userDataFolder: Path.Combine(_dataDir, "WebView2"));
            _closing.Token.ThrowIfCancellationRequested();
            _backend = new Backend(Path.Combine(AppContext.BaseDirectory, "server"));
            _origin = await _backend.Ready.WaitAsync(TimeSpan.FromSeconds(45), _closing.Token);
            _backend.Exited += BackendExited;
            if (_backend.HasExited) throw new InvalidOperationException("本地编辑器进程意外退出。");
            Content = _view;
            await _view.EnsureCoreWebView2Async(env);
            _closing.Token.ThrowIfCancellationRequested();

            var core = _view.CoreWebView2;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;
            core.Settings.AreHostObjectsAllowed = false;
            core.Settings.IsWebMessageEnabled = true;
            core.NavigationStarting += (_, args) => args.Cancel = !IsTrusted(args.Uri);
            core.FrameNavigationStarting += (_, args) => args.Cancel = !IsTrusted(args.Uri);
            core.NewWindowRequested += (_, args) => args.Handled = true;
            core.LaunchingExternalUriScheme += (_, args) => args.Cancel = true;
            core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All,
                CoreWebView2WebResourceRequestSourceKinds.All);
            core.WebResourceRequested += (_, args) =>
            {
                string uri = args.Request.Uri;
                // Locally generated preview images and Blob downloads do not contact another origin.
                if (!IsTrusted(uri) && !IsTrustedBlob(uri) &&
                    !(args.ResourceContext == CoreWebView2WebResourceContext.Image &&
                      uri.StartsWith("data:image/png;base64,", StringComparison.OrdinalIgnoreCase)))
                    args.Response = env.CreateWebResourceResponse(null, 403, "Forbidden", "");
            };
            core.WebMessageReceived += WebMessageReceived;
            core.DownloadStarting += DownloadStarting;
            core.NavigationCompleted += (_, args) =>
            {
                if (!args.IsSuccess && args.WebErrorStatus != CoreWebView2WebErrorStatus.OperationCanceled)
                    StartupError("无法打开本地编辑器：" + args.WebErrorStatus);
            };
            _library = ReadLibrary();
            _libraryScript = await core.AddScriptToExecuteOnDocumentCreatedAsync(LibraryScript(_library));
            _closing.Token.ThrowIfCancellationRequested();
            core.Navigate(_origin.AbsoluteUri);
        }
        catch (WebView2RuntimeNotFoundException)
        {
            StartupError("找不到 Microsoft Edge WebView2 Evergreen Runtime。请安装微软的 WebView2 Runtime 后重试。");
        }
        catch (Exception error) when (!_closing.IsCancellationRequested)
        {
            StartupError("无法启动 ShotDepth：" + error.Message);
        }
        catch (Exception) when (_closing.IsCancellationRequested) { }
    }

    private void BackendExited() => Dispatcher.BeginInvoke(() =>
    {
        if (!_closing.IsCancellationRequested) StartupError("本地编辑器进程意外退出。");
    });

    private void StartupError(string message)
    {
        if (_closing.IsCancellationRequested) return;
        string? details = _backend?.Diagnostics;
        _backend?.Dispose();
        _status.Text = string.IsNullOrWhiteSpace(details) ? message : message + "\n\n" + details;
        Content = _status;
    }

    private bool IsTrusted(string? value) => _origin is not null &&
        Uri.TryCreate(value, UriKind.Absolute, out var uri) &&
        uri.Scheme == Uri.UriSchemeHttp && uri.IdnHost == "127.0.0.1" &&
        uri.Port == _origin.Port && uri.UserInfo.Length == 0;

    private bool IsTrustedBlob(string uri) => uri.StartsWith("blob:", StringComparison.Ordinal) && IsTrusted(uri[5..]);

    private string LibraryScript(string? json) =>
        "if (window === window.top && location.origin === " +
        JsonSerializer.Serialize(_origin!.GetLeftPart(UriPartial.Authority)) +
        ") window.spikeMarkDesktopLibrary = " + JsonSerializer.Serialize(json) + ";";

    private string? ReadLibrary()
    {
        try
        {
            using var file = File.OpenRead(LibraryPath);
            if (file.Length > MaxLibraryBytes) return null;
            var data = new byte[(int)file.Length];
            file.ReadExactly(data);
            var json = Utf8.GetString(data);
            using var parsed = JsonDocument.Parse(json);
            return parsed.RootElement.ValueKind is JsonValueKind.Array or JsonValueKind.Object ? json : null;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or DecoderFallbackException)
        {
            return null;
        }
    }

    private async void WebMessageReceived(object? sender, CoreWebView2WebMessageReceivedEventArgs args)
    {
        var core = _view.CoreWebView2;
        if (_closing.IsCancellationRequested || !IsTrusted(core.Source) || !IsTrusted(args.Source)) return;
        bool reply = false, saved = false;
        try
        {
            string message = args.WebMessageAsJson;
            if (message.Length > MaxMessageChars)
            {
                reply = true;
                return;
            }
            using var parsed = JsonDocument.Parse(message);
            var root = parsed.RootElement;
            if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("type", out var type) ||
                type.ValueKind != JsonValueKind.String) return;
            switch (type.GetString())
            {
                case "scene-dirty":
                    if (root.TryGetProperty("dirty", out var dirty) &&
                        dirty.ValueKind is JsonValueKind.True or JsonValueKind.False)
                        _sceneDirty = dirty.GetBoolean();
                    return;
                case "library":
                    break;
                default:
                    return;
            }
            reply = true;
            if (!root.TryGetProperty("value", out var value)) return;
            string? json = value.ValueKind switch
            {
                JsonValueKind.Null => null,
                JsonValueKind.String => value.GetString(),
                JsonValueKind.Array or JsonValueKind.Object => value.GetRawText(),
                _ => throw new JsonException(),
            };
            if (json is not null)
            {
                if (json.Length > MaxLibraryBytes || Utf8.GetByteCount(json) > MaxLibraryBytes) return;
                using var payload = JsonDocument.Parse(json);
                if (payload.RootElement.ValueKind is not (JsonValueKind.Array or JsonValueKind.Object)) return;
                string temp = LibraryPath + ".tmp";
                File.WriteAllText(temp, json, Utf8);
                File.Move(temp, LibraryPath, overwrite: true);
            }
            else File.Delete(LibraryPath);
            saved = true;
            _library = json;
            if (_updatingScript) return;
            _updatingScript = true;
            try
            {
                // Add first while leaving the previous script active, then remove it.
                // Coalesce writes arriving while registration is in flight.
                string? installed;
                do
                {
                    installed = _library;
                    string replacement = await core.AddScriptToExecuteOnDocumentCreatedAsync(LibraryScript(installed));
                    if (_closing.IsCancellationRequested) return;
                    if (_libraryScript is not null) core.RemoveScriptToExecuteOnDocumentCreated(_libraryScript);
                    _libraryScript = replacement;
                } while (_library != installed);
            }
            finally { _updatingScript = false; }
        }
        catch (Exception error) when (error is JsonException or EncoderFallbackException or IOException or UnauthorizedAccessException or InvalidOperationException or COMException) { }
        finally
        {
            if (reply && !_closing.IsCancellationRequested && IsTrusted(core.Source))
                core.PostWebMessageAsJson(saved ? "{\"type\":\"library-saved\",\"ok\":true}" : "{\"type\":\"library-saved\",\"ok\":false}");
        }
    }

    private async void DownloadStarting(object? sender, CoreWebView2DownloadStartingEventArgs args)
    {
        args.Handled = true;
        args.Cancel = true;
        var core = _view.CoreWebView2;
        var operation = args.DownloadOperation;
        string url = operation.Uri;
        // Keep Chromium's full Blob URL unchanged for the page to correlate.
        bool trustedBlob = IsTrustedBlob(url);
        if (_closing.IsCancellationRequested || !IsTrusted(core.Source) ||
            (!trustedBlob && !IsTrusted(url))) return;

        string mimeType = operation.MimeType.Split(';', 2)[0].Trim();
        bool reportSceneExport = trustedBlob &&
            (string.Equals(mimeType, "application/json", StringComparison.OrdinalIgnoreCase) ||
             string.Equals(mimeType, "application/zip", StringComparison.OrdinalIgnoreCase));
        bool accepted, canceled = false;
        try
        {
            using var deferral = args.GetDeferral();
            accepted = await Dispatcher.InvokeAsync(() =>
            {
                if (_closing.IsCancellationRequested || !IsTrusted(core.Source)) return false;
                var dialog = new SaveFileDialog
                {
                    Title = "保存 ShotDepth 文件",
                    FileName = Path.GetFileName(args.ResultFilePath),
                    Filter = "ShotDepth 文件 (*.json;*.zip)|*.json;*.zip|所有文件 (*.*)|*.*",
                    OverwritePrompt = true,
                    AddExtension = true,
                    DefaultExt = Path.GetExtension(args.ResultFilePath),
                };
                if (dialog.ShowDialog(this) != true)
                {
                    canceled = true;
                    return false;
                }
                if (_closing.IsCancellationRequested || !IsTrusted(core.Source)) return false;
                args.ResultFilePath = dialog.FileName;
                args.Cancel = false;
                return true;
            });
        }
        catch (Exception error) when (error is OperationCanceledException or InvalidOperationException or COMException)
        {
            accepted = false;
        }

        if (!reportSceneExport || _closing.IsCancellationRequested) return;
        if (accepted) TrackSceneExport(core, operation, url);
        else PostSceneExport(core, url, ok: false, canceled: canceled);
    }

    private void TrackSceneExport(CoreWebView2 core, CoreWebView2DownloadOperation operation, string url)
    {
        void StateChanged(object? sender, object args)
        {
            if (_closing.IsCancellationRequested || !_sceneDownloads.ContainsKey(operation)) return;
            bool ok = false, canceled = false;
            try
            {
                var state = operation.State;
                if (state == CoreWebView2DownloadState.InProgress) return;
                ok = state == CoreWebView2DownloadState.Completed;
                canceled = state == CoreWebView2DownloadState.Interrupted &&
                    operation.InterruptReason == CoreWebView2DownloadInterruptReason.UserCanceled;
            }
            catch (Exception error) when (error is InvalidOperationException or COMException) { }
            _sceneDownloads.Remove(operation);
            DetachDownloadHandler(operation, StateChanged);
            PostSceneExport(core, url, ok, canceled);
        }

        try { operation.StateChanged += StateChanged; }
        catch (Exception error) when (error is InvalidOperationException or COMException)
        {
            PostSceneExport(core, url, ok: false);
            return;
        }
        _sceneDownloads.Add(operation, StateChanged);
        // The download may have finished before the subscription was installed.
        StateChanged(null, EventArgs.Empty);
    }

    private static void DetachDownloadHandler(CoreWebView2DownloadOperation operation, EventHandler<object> handler)
    {
        try { operation.StateChanged -= handler; }
        catch (Exception error) when (error is InvalidOperationException or COMException) { }
    }

    private void PostSceneExport(CoreWebView2 core, string url, bool ok, bool canceled = false)
    {
        if (_closing.IsCancellationRequested) return;
        try
        {
            if (IsTrusted(core.Source))
                core.PostWebMessageAsJson(JsonSerializer.Serialize(new { type = "scene-export", url, ok, canceled }));
        }
        catch (Exception error) when (error is InvalidOperationException or COMException) { }
    }
}
