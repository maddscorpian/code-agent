import * as vscode from "vscode";
import { ApiClient } from "./api_client";
import { ChatPanel } from "./chat_panel";

export function registerCodeActions(context: vscode.ExtensionContext) {
  const api = new ApiClient();

  context.subscriptions.push(
    vscode.commands.registerCommand("localai.explainSelection", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const selectedText = editor.document.getText(editor.selection);
      const filePath = editor.document.uri.fsPath;
      const panel = ChatPanel.createOrShow(context);
      await panel.sendPromptFromEditor(`Explain this code:\n${selectedText}\nFile: ${filePath}`, "chat");
    }),
    vscode.commands.registerCommand("localai.generateChange", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const question = await vscode.window.showInputBox({ prompt: "Describe the change you want to make" });
      if (!question) return;
      const panel = ChatPanel.createOrShow(context);
      await panel.sendPromptFromEditor(question, "generate");
    }),
    vscode.commands.registerCommand("localai.impactAnalysis", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const selected = editor.selection.isEmpty ? editor.document.getText() : editor.document.getText(editor.selection);
      const panel = ChatPanel.createOrShow(context);
      await panel.sendPromptFromEditor(`Analyze the impact of changing: ${selected}`, "impact");
    }),
    vscode.commands.registerCommand("localai.reindex", async () => {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Re-indexing codebase..." },
        async () => {
          const out = await api.reindex();
          vscode.window.showInformationMessage(
            `Re-index complete: ${out.projects_indexed.join(", ")} (${out.chunks_created} chunks)`
          );
        }
      );
    }),
    vscode.commands.registerCommand("localai.openChat", async () => {
      ChatPanel.createOrShow(context);
    })
  );
}
