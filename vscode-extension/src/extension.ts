import * as vscode from "vscode";
import { ChatPanel } from "./chat_panel";
import { registerCodeActions } from "./code_actions";
import { LocalAiCodeLensProvider } from "./inline_lens";

export function activate(context: vscode.ExtensionContext) {
  registerCodeActions(context);

  context.subscriptions.push(
    vscode.commands.registerCommand("localai.openChatFromLens", async (question: string) => {
      const panel = ChatPanel.createOrShow(context);
      await panel.sendPromptFromEditor(question, "chat");
    }),
    vscode.languages.registerCodeLensProvider(
      [{ language: "typescript" }, { language: "java" }],
      new LocalAiCodeLensProvider()
    )
  );
}

export function deactivate() {}
