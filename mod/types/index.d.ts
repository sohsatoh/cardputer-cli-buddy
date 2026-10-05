export type CardbuddyLoop = number

declare module 'claude-code' {
  interface PluginState {
    cardbuddy: { loop: CardbuddyLoop }
  }
}
