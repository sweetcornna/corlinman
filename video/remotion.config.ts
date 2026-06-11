import { Config } from "@remotion/cli/config";

Config.setVideoImageFormat("jpeg");
Config.setJpegQuality(92);
Config.setOverwriteOutput(true);
Config.setConcurrency(4);
// Noto Serif SC + the 3 textures take >30s to settle in headless Chromium.
// Bump generous default so renders don't bounce on first-time font loads.
Config.setDelayRenderTimeoutInMilliseconds(180000);
