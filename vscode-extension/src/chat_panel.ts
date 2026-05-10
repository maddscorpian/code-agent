import * as vscode from "vscode";
import { ApiClient } from "./api_client";
import * as fs from "fs";

export class ChatPanel {
  private static instance: ChatPanel | undefined;
  private panel: vscode.WebviewPanel;
  private api = new ApiClient();
  private mode = "chat";

  static createOrShow(context: vscode.ExtensionContext): ChatPanel {
    if (ChatPanel.instance) {
      ChatPanel.instance.panel.reveal(vscode.ViewColumn.Beside);
      return ChatPanel.instance;
    }
    const panel = vscode.window.createWebviewPanel("localai.chat", "Local AI Agent", vscode.ViewColumn.Beside, {
      enableScripts: true,
      retainContextWhenHidden: true,
    });
    ChatPanel.instance = new ChatPanel(panel, context);
    return ChatPanel.instance;
  }

  private constructor(panel: vscode.WebviewPanel, private context: vscode.ExtensionContext) {
    this.panel = panel;
    this.panel.webview.html = this.getHtml();
    this.panel.webview.onDidReceiveMessage(async (msg) => {
      if (msg.type === "ask") {
        await this.handleAsk(msg.question, msg.mode, msg.attachFile);
      } else if (msg.type === "openFile") {
        const doc = await vscode.workspace.openTextDocument(msg.filePath);
        await vscode.window.showTextDocument(doc);
      }
    });
  }

  async sendPromptFromEditor(selectedText: string, mode: string) {
    this.mode = mode;
    this.panel.webview.postMessage({ type: "prefill", text: selectedText, mode });
    await this.handleAsk(selectedText, mode, false);
  }

  private async handleAsk(question: string, mode: string, attachFile: boolean) {
    const editor = vscode.window.activeTextEditor;
    const fileContext = attachFile && editor ? editor.document.getText() : undefined;
    this.panel.webview.postMessage({ type: "startAi" });
    let answer = "";
    await this.api.askStream(question, mode, fileContext, (token) => {
      answer += token;
      this.panel.webview.postMessage({ type: "token", token });
    });
    const full = await this.api.ask(question, mode, fileContext);
    this.panel.webview.postMessage({ type: "doneAi", answer, sources: full.sources || [] });
  }

  private getHtml(): string {
    const htmlPath = vscode.Uri.file(`${this.context.extensionPath}/media/chat.html`);
    const cssPath = vscode.Uri.file(`${this.context.extensionPath}/media/chat.css`);
    let html = fs.readFileSync(htmlPath.fsPath, "utf8");
    html = html.split("{{styleUri}}").join(this.panel.webview.asWebviewUri(cssPath).toString());
    return (
      html +
      `
<script>
const vscode = acquireVsCodeApi();
let mode = "chat";
const input = document.getElementById("input");
const attach = document.getElementById("attachFile");
const messages = document.getElementById("messages");
document.querySelectorAll(".tabs button").forEach(btn => {
  btn.addEventListener("click", () => mode = btn.dataset.mode);
});
document.getElementById("sendBtn").addEventListener("click", () => {
  const q = input.value.trim();
  if (!q) return;
  addMsg("user", q);
  vscode.postMessage({type:"ask", question:q, mode, attachFile: attach.checked});
  input.value = "";
});
window.addEventListener("message", ev => {
  const msg = ev.data;
  if (msg.type === "token") appendToken(msg.token);
  if (msg.type === "startAi") addMsg("ai", "");
  if (msg.type === "doneAi") addSources(msg.sources || []);
  if (msg.type === "prefill") { input.value = msg.text; mode = msg.mode || mode; }
});
function addMsg(kind, txt) {
  const div = document.createElement("div");
  div.className = "msg " + kind;
  div.innerHTML = kind === "ai" ? (window.marked ? marked.parse(txt) : txt) : txt;
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}
function appendToken(t) {
  const last = messages.lastElementChild;
  if (!last) return;
  last.textContent += t;
  messages.scrollTop = messages.scrollHeight;
}
function addSources(sources) {
  if (!sources.length) return;
  const wrap = document.createElement("details");
  wrap.innerHTML = "<summary>Sources</summary>";
  sources.forEach(s => {
    const a = document.createElement("a");
    a.href = "#";
    a.textContent = (s.project || "unknown") + ": " + (s.file_path || "digest");
    a.onclick = (e) => { e.preventDefault(); vscode.postMessage({type:"openFile", filePath: s.file_path}); };
    const line = document.createElement("div");
    line.appendChild(a);
    wrap.appendChild(line);
  });
  messages.appendChild(wrap);
}
</script>`
    );
  }
}
