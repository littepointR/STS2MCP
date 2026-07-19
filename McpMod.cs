using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Multiplayer.Game;

namespace STS2_MCP;

[ModInitializer("Initialize")]
public static partial class McpMod
{
    public const string Version = "0.3.4";
    public const int DefaultPort = 15526;
    internal const int MainThreadRequestTimeoutMilliseconds = 8000;
    private const string ConfigFileName = "STS2_MCP.conf";

    private static HttpListener? _listener;
    private static Thread? _serverThread;
    private static readonly ConcurrentQueue<MainThreadWorkItem> _mainThreadQueue = new();
    private static int _mainThreadWorkInFlight;
    internal static readonly JsonSerializerOptions _jsonOptions = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping
    };

    private static int LoadPort()
    {
        try
        {
            string? modDir = Path.GetDirectoryName(
                System.Reflection.Assembly.GetExecutingAssembly().Location);
            if (modDir == null) return DefaultPort;

            string configPath = Path.Combine(modDir, ConfigFileName);
            if (!File.Exists(configPath))
            {
                try
                {
                    var defaultConfig = new Dictionary<string, object> { ["port"] = DefaultPort };
                    string json = JsonSerializer.Serialize(defaultConfig, _jsonOptions);
                    File.WriteAllText(configPath, json);
                    GD.Print($"[STS2 MCP] Created default config at {configPath}");
                }
                catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
                {
                    GD.Print($"[STS2 MCP] No config found at {configPath}; using default port {DefaultPort}");
                }
                return DefaultPort;
            }

            string content = File.ReadAllText(configPath);
            using var doc = JsonDocument.Parse(content);
            if (doc.RootElement.TryGetProperty("port", out var portElem)
                && portElem.TryGetInt32(out int port)
                && port is > 0 and <= 65535)
            {
                return port;
            }

            GD.PrintErr($"[STS2 MCP] Invalid or missing 'port' in {configPath}, using default {DefaultPort}");
            return DefaultPort;
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[STS2 MCP] Failed to load config: {ex.Message}, using default port {DefaultPort}");
            return DefaultPort;
        }
    }

    public static void Initialize()
    {
        try
        {
            // Optional settings UI patches should not block the HTTP bridge itself.
            TryApplyHarmonyPatches();

            // Connect to main thread process frame for action execution
            var tree = (SceneTree)Engine.GetMainLoop();
            tree.Connect(SceneTree.SignalName.ProcessFrame, Callable.From(ProcessMainThreadQueue));

            int port = LoadPort();

            _listener = new HttpListener();
            _listener.Prefixes.Add($"http://localhost:{port}/");
            _listener.Prefixes.Add($"http://127.0.0.1:{port}/");
            _listener.Start();

            _serverThread = new Thread(ServerLoop)
            {
                IsBackground = true,
                Name = "STS2_MCP_Server"
            };
            _serverThread.Start();

            GD.Print($"[STS2 MCP] v{Version} server started on http://localhost:{port}/");
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[STS2 MCP] Failed to start: {ex}");
        }
    }

    private static void TryApplyHarmonyPatches()
    {
        try
        {
            new Harmony("com.sts2mcp").PatchAll();
        }
        catch (Exception ex)
        {
            GD.Print(
                $"[STS2 MCP] Optional Harmony settings UI injection skipped: {ex.GetType().Name}: {ex.Message}");
        }
    }

    private sealed class MainThreadWorkItem
    {
        private readonly Action _action;
        private readonly Action _onSkipped;
        private int _state;

        public MainThreadWorkItem(Action action, Action onSkipped)
        {
            _action = action;
            _onSkipped = onSkipped;
        }

        public void Cancel() => Interlocked.CompareExchange(ref _state, 2, 0);

        public bool TryExecute()
        {
            if (Interlocked.CompareExchange(ref _state, 1, 0) != 0)
            {
                _onSkipped();
                return false;
            }

            _action();
            Volatile.Write(ref _state, 3);
            return true;
        }
    }

    internal sealed class MainThreadRequestTimeoutException : TimeoutException
    {
        public MainThreadRequestTimeoutException()
            : base($"Main-thread request timed out after {MainThreadRequestTimeoutMilliseconds} ms") { }
    }

    internal sealed class MainThreadRequestBusyException : InvalidOperationException
    {
        public MainThreadRequestBusyException()
            : base("Another main-thread request is still running") { }
    }

    private static void ProcessMainThreadQueue()
    {
        int processed = 0;
        while (processed < 10 && _mainThreadQueue.TryDequeue(out var workItem))
        {
            try { if (workItem.TryExecute()) processed++; }
            catch (Exception ex) { GD.PrintErr($"[STS2 MCP] Main thread action error: {ex}"); }
        }
    }

    private static MainThreadWorkItem EnqueueMainThreadWork(Action action)
    {
        if (Interlocked.CompareExchange(ref _mainThreadWorkInFlight, 1, 0) != 0)
            throw new MainThreadRequestBusyException();

        var workItem = new MainThreadWorkItem(action, ReleaseMainThreadWork);
        _mainThreadQueue.Enqueue(workItem);
        return workItem;
    }

    private static void ReleaseMainThreadWork()
    {
        Volatile.Write(ref _mainThreadWorkInFlight, 0);
    }

    private static T WaitForMainThread<T>(Task<T> task, MainThreadWorkItem workItem)
    {
        using var timeout = new CancellationTokenSource();
        var timeoutTask = Task.Delay(MainThreadRequestTimeoutMilliseconds, timeout.Token);
        var completedTask = Task.WhenAny(task, timeoutTask).GetAwaiter().GetResult();
        if (!ReferenceEquals(completedTask, task))
        {
            workItem.Cancel();
            throw new MainThreadRequestTimeoutException();
        }

        timeout.Cancel();
        return task.GetAwaiter().GetResult();
    }

    internal static T RunOnMainThreadAndWait<T>(Func<T> func)
    {
        var tcs = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);
        var workItem = EnqueueMainThreadWork(() =>
        {
            try { tcs.SetResult(func()); }
            catch (Exception ex) { tcs.SetException(ex); }
            finally { ReleaseMainThreadWork(); }
        });
        return WaitForMainThread(tcs.Task, workItem);
    }

    internal static T RunOnMainThreadAndWaitAsync<T>(Func<Task<T>> func)
    {
        var tcs = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);
        var workItem = EnqueueMainThreadWork(() =>
        {
            Task<T> asyncTask;
            try
            {
                asyncTask = func();
            }
            catch (Exception ex)
            {
                tcs.SetException(ex);
                ReleaseMainThreadWork();
                return;
            }

            _ = asyncTask.ContinueWith(
                completed =>
                {
                    try
                    {
                        if (completed.IsCanceled)
                            tcs.TrySetCanceled();
                        else if (completed.IsFaulted)
                            tcs.TrySetException(completed.Exception!.InnerExceptions);
                        else
                            tcs.TrySetResult(completed.Result);
                    }
                    finally
                    {
                        ReleaseMainThreadWork();
                    }
                },
                CancellationToken.None,
                TaskContinuationOptions.ExecuteSynchronously,
                TaskScheduler.Default);
        });
        return WaitForMainThread(tcs.Task, workItem);
    }

    internal static void RunOnMainThreadAndWait(Action action)
    {
        RunOnMainThreadAndWait(() =>
        {
            action();
            return true;
        });
    }

    private static void ServerLoop()
    {
        while (_listener?.IsListening == true)
        {
            try
            {
                var context = _listener.GetContext();
                // Handle each request asynchronously so we don't block the listener
                ThreadPool.QueueUserWorkItem(_ => HandleRequest(context));
            }
            catch (HttpListenerException) { break; }
            catch (ObjectDisposedException) { break; }
        }
    }

    private static void HandleRequest(HttpListenerContext context)
    {
        try
        {
            var request = context.Request;
            var response = context.Response;
            response.Headers.Add("Access-Control-Allow-Origin", "*");
            response.Headers.Add("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
            response.Headers.Add("Access-Control-Allow-Headers", "Content-Type");

            if (request.HttpMethod == "OPTIONS")
            {
                response.StatusCode = 204;
                response.Close();
                return;
            }

            string path = request.Url?.AbsolutePath ?? "/";

            if (path == "/")
            {
                SendJson(response, new { message = $"Hello from STS2 MCP v{Version}", status = "ok" });
            }
            else if (path == "/api/v1/singleplayer")
            {
                // Hard-block singleplayer endpoint during multiplayer runs
                // to prevent calling the non-sync-safe end_turn path
                if (IsMultiplayerRun())
                {
                    SendError(response, 409,
                        "Multiplayer run is active. Use /api/v1/multiplayer instead.");
                    return;
                }

                if (request.HttpMethod == "GET")
                    HandleGetState(request, response);
                else if (request.HttpMethod == "POST")
                    HandlePostAction(request, response);
                else
                    SendError(response, 405, "Method not allowed");
            }
            else if (path == "/api/v1/multiplayer")
            {
                // Guard: reject multiplayer endpoint during singleplayer runs
                if (!IsMultiplayerRun())
                {
                    SendError(response, 409,
                        "Not in a multiplayer run. Use /api/v1/singleplayer instead.");
                    return;
                }

                if (request.HttpMethod == "GET")
                    HandleGetMultiplayerState(request, response);
                else if (request.HttpMethod == "POST")
                    HandlePostMultiplayerAction(request, response);
                else
                    SendError(response, 405, "Method not allowed");
            }
            else if (path == "/api/v1/profiles")
            {
                if (request.HttpMethod == "GET")
                    HandleGetProfiles(response);
                else if (request.HttpMethod == "POST")
                    HandlePostProfiles(request, response);
                else
                    SendError(response, 405, "Method not allowed");
            }
            else if (path == "/api/v1/profile")
            {
                if (request.HttpMethod == "GET")
                    HandleGetProfile(response);
                else
                    SendError(response, 405, "Method not allowed");
            }
            else
            {
                SendError(response, 404, "Not found");
            }
        }
        catch (Exception ex)
        {
            try
            {
                SendError(context.Response, 500, $"Internal error: {ex.Message}");
            }
            catch { /* response may already be closed */ }
        }
    }

    // Called on HTTP thread (not main thread) as a best-effort guard.
    // The try/catch handles race conditions during run transitions.
    // Authoritative checks happen inside RunOnMainThread lambdas.
    internal static bool IsMultiplayerRun()
    {
        try
        {
            return MegaCrit.Sts2.Core.Runs.RunManager.Instance.IsInProgress
                && MegaCrit.Sts2.Core.Runs.RunManager.Instance.NetService.Type.IsMultiplayer();
        }
        catch { return false; }
    }

    private static void HandleGetMultiplayerState(HttpListenerRequest request, HttpListenerResponse response)
    {
        string format = request.QueryString["format"] ?? "json";

        try
        {
            var state = RunOnMainThreadAndWait(() => BuildMultiplayerGameState());

            if (format == "markdown")
            {
                string md = FormatAsMarkdown(state);
                SendText(response, md, "text/markdown");
            }
            else
            {
                SendJson(response, state);
            }
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[STS2 MCP] HandleGetMultiplayerState: {ex}");
            try
            {
                SendError(response, 500, $"Failed to read multiplayer game state: {ex.Message}", ex);
            }
            catch { /* response may be unusable */ }
        }
    }

    private static void HandlePostMultiplayerAction(HttpListenerRequest request, HttpListenerResponse response)
    {
        string body;
        using (var reader = new StreamReader(request.InputStream, request.ContentEncoding))
            body = reader.ReadToEnd();

        Dictionary<string, JsonElement>? parsed;
        try
        {
            parsed = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(body);
        }
        catch
        {
            SendError(response, 400, "Invalid JSON");
            return;
        }

        if (parsed == null || !parsed.TryGetValue("action", out var actionElem))
        {
            SendError(response, 400, "Missing 'action' field");
            return;
        }

        string action = actionElem.GetString() ?? "";

        try
        {
            var result = RunOnMainThreadAndWait(() => ExecuteMultiplayerAction(action, parsed));
            SendJson(response, result);
        }
        catch (Exception ex)
        {
            SendError(response, 500, $"Multiplayer action failed: {ex.Message}", ex);
        }
    }

    private static void HandleGetState(HttpListenerRequest request, HttpListenerResponse response)
    {
        string format = request.QueryString["format"] ?? "json";

        try
        {
            var state = RunOnMainThreadAndWait(() => BuildGameState());

            if (format == "markdown")
            {
                try
                {
                    SendText(response, FormatAsMarkdown(state), "text/markdown");
                }
                catch (Exception ex)
                {
                    GD.PrintErr($"[STS2 MCP] FormatAsMarkdown failed, returning JSON: {ex}");
                    SendJson(response, state);
                }
            }
            else
            {
                SendJson(response, state);
            }
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[STS2 MCP] HandleGetState: {ex}");
            try
            {
                SendError(response, 500, $"Failed to read game state: {ex.Message}", ex);
            }
            catch { /* response may be unusable */ }
        }
    }

    private static void HandlePostAction(HttpListenerRequest request, HttpListenerResponse response)
    {
        string body;
        using (var reader = new StreamReader(request.InputStream, request.ContentEncoding))
            body = reader.ReadToEnd();

        Dictionary<string, JsonElement>? parsed;
        try
        {
            parsed = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(body);
        }
        catch
        {
            SendError(response, 400, "Invalid JSON");
            return;
        }

        if (parsed == null || !parsed.TryGetValue("action", out var actionElem))
        {
            SendError(response, 400, "Missing 'action' field");
            return;
        }

        string action = actionElem.GetString() ?? "";

        if (action == "restart_combat")
        {
            try
            {
                var result = RunOnMainThreadAndWaitAsync(ExecuteRestartCombatAsync);
                SendJson(response, result);
            }
            catch (Exception ex)
            {
                SendError(response, 500, $"Combat restart failed: {ex.Message}", ex);
            }
            return;
        }

        if (action == "resolve_pending_epochs")
        {
            try
            {
                var result = RunOnMainThreadAndWait(ExecuteResolvePendingEpochs);
                SendJson(response, result);
            }
            catch (Exception ex)
            {
                SendError(response, 500, $"Profile initialization failed: {ex.Message}", ex);
            }
            return;
        }

        if (action == "menu_select")
        {
            try
            {
                var option = parsed.TryGetValue("option", out var optElem) ? optElem.GetString() ?? "" : "";
                var seed = parsed.TryGetValue("seed", out var seedElem) ? seedElem.GetString() : null;
                var result = RunOnMainThreadAndWait(() => ExecuteMenuSelect(option, seed));
                SendJson(response, result);
            }
            catch (Exception ex)
            {
                SendError(response, 500, $"Menu action failed: {ex.Message}", ex);
            }
            return;
        }

        try
        {
            var result = RunOnMainThreadAndWait(() => ExecuteAction(action, parsed));
            SendJson(response, result);
        }
        catch (Exception ex)
        {
            SendError(response, 500, $"Action failed: {ex.Message}", ex);
        }
    }
}
