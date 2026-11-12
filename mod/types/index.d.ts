export type CardbuddyLoop = string

declare module 'claude-code' {
  interface PluginState {
    cardbuddy: { loop: CardbuddyLoop }
  }
}
