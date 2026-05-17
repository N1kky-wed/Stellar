# Mobile Development Mandate (React Native & Android)

When you are asked to build a mobile application (Android/APK), you MUST use `repo_control` with `env_type="mobile"`. This provisions a specialized Docker container equipped with the Android SDK, Java, and Node.js.

## 1. Project Initialization & Scaffold

1.  **Use `repo_control(action='deploy', env_type='mobile', project_name='Your App Name', port=5000)`** to provision the mobile environment.
2.  Once deployed, use `repo_control(action='execute')` to create the React Native / Expo application. The best way to create a clean, robust mobile app is via Expo:
    ```bash
    npx create-expo-app@latest my-app --template blank
    ```
    *If it prompts or gets stuck, ensure you pass necessary flags to prevent interactive prompts or explicitly use yarn/npm.*

## 2. Web Preview for the Chat Interface (Crucial)

Before compiling the native APK, you must verify the logic using Expo's web bundler, which can be previewed directly in the Stellar chat UI as an iframe.
1.  Ensure `react-native-web` and `react-dom` are installed:
    ```bash
    cd my-app && npx expo install react-native-web react-dom @expo/metro-runtime
    ```
2.  Start the web preview. You MUST bind it to port 5000 and ALWAYS clear the port first to prevent hanging conflicts:
    ```bash
    pkill -9 -f node || true
    cd my-app && nohup npx expo start --web --port 5000 > build.log 2>&1 &
    ```
3.  Once started, provide the user with the deployed subdomain link (e.g., `https://your-app-name.stellarai.live`) so they can test the mobile app logic inside the browser.

## 3. Compiling the Native APK

Once the user approves the preview, compile the actual Android `.apk`.
Since you are inside a fully-equipped Android container:
1.  Generate the native Android project files (prebuild):
    ```bash
    cd my-app && npx expo prebuild --platform android --clean
    ```
2.  Build the release APK using Gradle:
    ```bash
    cd my-app/android && ./gradlew assembleRelease
    ```
    *Note: This process takes several minutes. Use a background process or warn the user about the build time.*
3.  Once the build succeeds, the APK will be located at:
    `my-app/android/app/build/outputs/apk/release/app-release.apk`

## 4. Delivering the APK

To deliver the APK to the user, copy it to the container's accessible web root or transfer it to `/lab` and link it.
The easiest method is to copy it to a directory accessible via Python's HTTP server or move it to a known download path:
```bash
cp my-app/android/app/build/outputs/apk/release/app-release.apk /app/app-release.apk
```
Or you can use `manage_files(action='move')` to place it in `/lab` so the user can easily download it.
Provide the user with a direct hyperlink to download the APK.

## 5. Summary of Important Rules

-   **ALWAYS** use `repo_control` with `env_type="mobile"`.
-   **ALWAYS** provide a web preview first using `npx expo start --web`.
-   **DO NOT** attempt to use `eas build` as it requires user authentication; always use `npx expo prebuild` followed by `./gradlew assembleRelease` for local container builds.
-   Be mindful of interactive prompts in npm/yarn/expo. Always use `-y`, `--yes`, `--no-interactive` flags where applicable.