import { app } from "../../scripts/app.js";
import { setupDramaWorkbench } from "./drama/workbench.js";

app.registerExtension({
  name: "dgx.drama.workbench",
  async setup() {
    if (new URLSearchParams(location.search).get("dramaUI") !== "legacy") return;
    await setupDramaWorkbench();
  },
});
