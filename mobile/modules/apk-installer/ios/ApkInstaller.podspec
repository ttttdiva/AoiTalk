require 'json'

package = JSON.parse(File.read(File.join(__dir__, '..', 'package.json'))) rescue {}

Pod::Spec.new do |s|
  s.name         = 'ApkInstaller'
  s.version      = package['version'] || '0.1.0'
  s.summary      = 'APK installer module placeholder'
  s.description  = 'Android APK installer bridge placeholder for iOS'
  s.author       = 'OpenAI'
  s.homepage     = 'https://github.com/ttttdiva/AoiTalk'
  s.platforms    = { :ios => '15.1' }
  s.source       = { :git => 'https://github.com/ttttdiva/AoiTalk.git' }
  s.static_framework = true
  s.dependency 'ExpoModulesCore'
  s.source_files = 'ios/**/*.{h,m,mm,swift}'
end
