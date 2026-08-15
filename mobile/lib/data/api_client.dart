// MARSOUD-MOBILE-FLUTTER — Dio-based bearer HTTP client.
//
// One instance app-wide, injected everywhere via Riverpod. Handles:
//   - Base URL from Env.apiBaseUrl.
//   - Authorization: Bearer <token> injected from AuthState.
//   - ?company_id=N injected from the current active company.
//   - 401 → clears token + navigates back to /login (via callback).
//   - Uniform error shape: throws ApiException with .status + .message.
import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';

import '../app/env.dart';

class ApiException implements Exception {
  final int status;
  final String message;
  final dynamic body;
  ApiException(this.status, this.message, {this.body});
  @override
  String toString() => 'ApiException($status): $message';
}

typedef TokenReader = String? Function();
typedef ActiveCompanyReader = int? Function();
typedef UnauthorizedHook = void Function();

class ApiClient {
  final Dio _dio;
  final TokenReader _readToken;
  final ActiveCompanyReader _readCompanyId;
  final UnauthorizedHook _onUnauthorized;

  ApiClient({
    required TokenReader readToken,
    required ActiveCompanyReader readCompanyId,
    required UnauthorizedHook onUnauthorized,
  })  : _readToken = readToken,
        _readCompanyId = readCompanyId,
        _onUnauthorized = onUnauthorized,
        _dio = Dio(BaseOptions(
          baseUrl: Env.apiBaseUrl,
          connectTimeout: const Duration(seconds: 12),
          receiveTimeout: const Duration(seconds: 25),
          sendTimeout: const Duration(seconds: 20),
          contentType: 'application/json',
          responseType: ResponseType.json,
        )) {
    _dio.interceptors.add(_AuthInterceptor(
      readToken: _readToken,
      readCompanyId: _readCompanyId,
    ));
    if (kDebugMode) {
      _dio.interceptors.add(LogInterceptor(
        request: false,
        requestHeader: false,
        requestBody: true,
        responseHeader: false,
        responseBody: false,
        error: true,
      ));
    }
  }

  Future<dynamic> get(String path, {Map<String, dynamic>? query}) async {
    return _run(() => _dio.get(path, queryParameters: query));
  }

  Future<dynamic> post(String path,
      {Object? body, Map<String, dynamic>? query}) async {
    return _run(() => _dio.post(path, data: body, queryParameters: query));
  }

  Future<Response<T>> raw<T>(String path, {ResponseType? responseType,
      Options? options, Map<String, dynamic>? query}) async {
    final r = await _dio.get<T>(path,
        queryParameters: query,
        options: (options ?? Options()).copyWith(responseType: responseType));
    return r;
  }

  Future<dynamic> _run(Future<Response> Function() fn) async {
    try {
      final r = await fn();
      return r.data;
    } on DioException catch (e) {
      final status = e.response?.statusCode ?? 0;
      if (status == 401) {
        _onUnauthorized();
      }
      final body = e.response?.data;
      final message = _errorMessage(body) ?? _networkMessage(e);
      throw ApiException(status, message, body: body);
    }
  }

  String? _errorMessage(dynamic body) {
    if (body is Map) {
      final err = body['error'] ?? body['message'];
      if (err is String && err.isNotEmpty) return err;
    }
    return null;
  }

  String _networkMessage(DioException e) {
    switch (e.type) {
      case DioExceptionType.connectionTimeout:
      case DioExceptionType.receiveTimeout:
      case DioExceptionType.sendTimeout:
        return 'انتهت مهلة الاتصال';
      case DioExceptionType.connectionError:
        return 'تعذّر الاتصال بالخادم';
      case DioExceptionType.cancel:
        return 'تم إلغاء الطلب';
      case DioExceptionType.badResponse:
        return 'خطأ من الخادم (${e.response?.statusCode})';
      case DioExceptionType.badCertificate:
        return 'شهادة SSL غير موثوقة';
      case DioExceptionType.unknown:
        return e.message ?? 'خطأ غير معروف';
      default:
        return e.message ?? 'خطأ غير معروف';
    }
  }
}

class _AuthInterceptor extends Interceptor {
  final TokenReader readToken;
  final ActiveCompanyReader readCompanyId;
  _AuthInterceptor({required this.readToken, required this.readCompanyId});

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    final tok = readToken();
    if (tok != null && tok.isNotEmpty) {
      options.headers['Authorization'] = 'Bearer $tok';
    }
    final cid = readCompanyId();
    // Only attach company_id when the caller hasn't overridden it AND
    // it's not one of the auth endpoints (login doesn't take one yet).
    if (cid != null &&
        !options.queryParameters.containsKey('company_id') &&
        !options.path.startsWith('/api/v1/auth/')) {
      options.queryParameters['company_id'] = cid;
    }
    super.onRequest(options, handler);
  }
}
