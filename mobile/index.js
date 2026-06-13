if (typeof global.WeakRef === 'undefined') {
  global.WeakRef = class WeakRefShim {
    constructor(value) {
      this._value = value;
    }

    deref() {
      return this._value;
    }
  };
}

if (typeof global.FinalizationRegistry === 'undefined') {
  global.FinalizationRegistry = class FinalizationRegistryShim {
    register() {}

    unregister() {
      return true;
    }
  };
}

import 'expo-router/entry';
